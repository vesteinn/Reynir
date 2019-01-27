#!/usr/bin/env python
"""

    Reynir: Natural language processing for Icelandic

    Tree module

    Copyright (C) 2018 Miðeind ehf.

       This program is free software: you can redistribute it and/or modify
       it under the terms of the GNU General Public License as published by
       the Free Software Foundation, either version 3 of the License, or
       (at your option) any later version.
       This program is distributed in the hope that it will be useful,
       but WITHOUT ANY WARRANTY; without even the implied warranty of
       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
       GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see http://www.gnu.org/licenses/.


    This module implements a data structure for parsed sentence trees that can
    be loaded from text strings and processed by plug-in processing functions.

    A set of provided utility functions allow the extraction of nominative, indefinite
    and canonical (nominative + indefinite + singular) forms of the text within any subtree.

"""

import json
import re

from contextlib import closing
from collections import OrderedDict, namedtuple

from settings import Settings, DisallowedNames, VerbObjects
from reynir.bindb import BIN_Db
from reynir.binparser import BIN_Token
from reynir.matcher import SimpleTreeBuilder
from reynir.cache import LRU_Cache


BIN_ORDFL = {
    "no": {"kk", "kvk", "hk"},
    "kk": {"kk"},
    "kvk": {"kvk"},
    "hk": {"hk"},
    "sérnafn": {"kk", "kvk", "hk"},
    "so": {"so"},
    "lo": {"lo"},
    "fs": {"fs"},
    "ao": {"ao"},
    "eo": {"ao"},
    "spao": {"ao"},
    "tao": {"ao"},
    "töl": {"töl", "to"},
    "to": {"töl", "to"},
    "fn": {"fn"},
    "pfn": {"pfn"},
    "st": {"st"},
    "stt": {"st"},
    "abfn": {"abfn"},
    "gr": {"gr"},
    "uh": {"uh"},
    "nhm": {"nhm"},
}

_REPEAT_SUFFIXES = frozenset(("+", "*", "?"))


class Result:

    """ Container for results that are sent from child nodes to parent nodes.
        This class is instrumented so that it is equivalent to use attribute
        or indexing notation, i.e. r.efliður is the same as r["efliður"].

        Additionally, the class implements lazy evaluation of the r._root,
        r._nominative and similar built-in attributes so that they are only calculated when
        and if required, and then cached. This is an optimization to save database
        reads.
    """

    def __init__(self, node, state, params):
        self.dict = dict()  # Our own custom dict for instance attributes
        self._node = node
        self._state = state
        self._params = params

    def __repr__(self):
        return "Result with {0} params\nDict is: {1}".format(
            len(self._params) if self._params else 0, self.dict
        )

    def __setattr__(self, key, val):
        """ Fancy attribute setter using our own dict for instance attributes """
        if key == "__dict__" or key == "dict" or key in self.__dict__:
            # Relay to Python's default attribute resolution mechanism
            super().__setattr__(key, val)
        else:
            # Set attribute in our own dict
            self.dict[key] = val

    def __getattr__(self, key):
        """ Fancy attribute getter with special cases for _root and _nominative """
        if key == "__dict__" or key == "dict" or key in self.__dict__:
            # Relay to Python's default attribute resolution mechanism
            return super().__getattr__(key)
        d = self.dict
        if key in d:
            return d[key]
        # Key not found: try lazy evaluation
        if key == "_root":
            # Lazy evaluation of the _root attribute
            # (Note that it can be overridden by setting it directly)
            d[key] = val = self._node.root(self._state, self._params)
            return val
        if key == "_nominative":
            # Lazy evaluation of the _nominative attribute
            # (Note that it can be overridden by setting it directly)
            d[key] = val = self._node.nominative(self._state, self._params)
            return val
        if key == "_indefinite":
            # Lazy evaluation of the _indefinite attribute
            # (Note that it can be overridden by setting it directly)
            d[key] = val = self._node.indefinite(self._state, self._params)
            return val
        if key == "_canonical":
            # Lazy evaluation of the _canonical attribute
            # (Note that it can be overridden by setting it directly)
            d[key] = val = self._node.canonical(self._state, self._params)
            return val
        # Not found in our custom dict:
        # hand off to Python's default attribute resolution mechanism
        return super().__getattr__(key)

    def __contains__(self, key):
        return key in self.dict

    def __getitem__(self, key):
        return self.dict[key]

    def __setitem__(self, key, val):
        self.dict[key] = val

    def __delitem__(self, key):
        del self.dict[key]

    def get(self, key, default=None):
        return self.dict.get(key, default)

    def attribs(self):
        """ Enumerate all attributes, and values, of this result object """
        for key, val in self.dict.items():
            yield (key, val)

    def user_attribs(self):
        """ Enumerate all user-defined attributes and values of this result object """
        for key, val in self.dict.items():
            if isinstance(key, str) and not key.startswith("_") and not callable(val):
                yield (key, val)

    def set(self, key, val):
        """ Set the key to the value, unless it has already been assigned """
        d = self.dict
        if key not in d:
            d[key] = val

    def copy_from(self, p):
        """ Copy all user attributes from p into this result """
        if p is self or p is None:
            return
        d = self.dict
        for key, val in p.user_attribs():
            # Pass all named parameters whose names do not start with an underscore
            # up to the parent, by default
            # Generally we have left-to-right priority, i.e.
            # the leftmost entity wins in case of conflict.
            # However, lists, sets and dictionaries with the same
            # member name are combined.
            if key not in d:
                d[key] = val
            else:
                # Combine lists and dictionaries
                left = d[key]
                if isinstance(left, list) and isinstance(val, list):
                    # Extend lists
                    left.extend(val)
                elif isinstance(left, set) and isinstance(val, set):
                    # Return union of sets
                    left |= val
                elif isinstance(left, dict) and isinstance(val, dict):
                    # Keep the left entries but add any new/additional val entries
                    # (This gives left priority; left.update(val) would give right priority)
                    d[key] = dict(val, **left)

    def del_attribs(self, alist):
        """ Delete the attribs in alist from the result object """
        if isinstance(alist, str):
            alist = (alist,)
        d = self.dict
        for a in alist:
            if a in d:
                del d[a]

    def enum_children(self, test_f=None):
        """ Enumerate the child parameters of this node, yielding (child_node, result)
            where the child node meets the given test, if any """
        if self._params:
            for p, c in zip(self._params, self._node.children()):
                if test_f is None or test_f(c):
                    yield (c, p)

    def enum_descendants(self, test_f=None):
        """ Enumerate the descendant parameters of this node, yielding (child_node, result)
            where the child node meets the given test, if any """
        if self._params:
            for p, c in zip(self._params, self._node.children()):
                if p is not None:
                    # yield from p.enum_descendants(test_f)
                    for d_c, d_p in p.enum_descendants(test_f):
                        yield (d_c, d_p)
                if test_f is None or test_f(c):
                    yield (c, p)

    def find_child(self, **kwargs):
        """ Find a child parameter meeting the criteria given in kwargs """

        def test_f(c):
            for key, val in kwargs.items():
                f = getattr(c, "has_" + key, None)
                if f is None or not f(val):
                    return False
            return True

        for c, p in self.enum_children(test_f):
            # Found a child node meeting the criteria: return its associated param
            return p
        # No child node found: return None
        return None

    def all_children(self, **kwargs):
        """ Return all child parameters meeting the criteria given in kwargs """

        def test_f(c):
            for key, val in kwargs.items():
                f = getattr(c, "has_" + key, None)
                if f is None or not f(val):
                    return False
            return True

        return [p for _, p in self.enum_children(test_f)]

    def find_descendant(self, **kwargs):
        """ Find a descendant parameter meeting the criteria given in kwargs """

        def test_f(c):
            for key, val in kwargs.items():
                f = getattr(c, "has_" + key, None)
                if f is None or not f(val):
                    return False
            return True

        for c, p in self.enum_descendants(test_f):
            # Found a child node meeting the criteria: return its associated param
            return p
        # No child node found: return None
        return None

    def has_nt_base(self, s):
        """ Does the associated node have the given nonterminal base name? """
        return self._node.has_nt_base(s)

    def has_t_base(self, s):
        """ Does the associated node have the given terminal base name? """
        return self._node.has_t_base(s)

    def has_variant(self, s):
        """ Does the associated node have the given variant? """
        return self._node.has_variant(s)


class Node:

    """ Base class for terminal and nonterminal nodes reconstructed from
        trees in text format loaded from the scraper database """

    def __init__(self):
        self.child = None
        self.nxt = None

    def set_next(self, n):
        self.nxt = n

    def set_child(self, n):
        self.child = n

    def has_nt_base(self, s):
        """ Does the node have the given nonterminal base name? """
        return False

    def has_t_base(self, s):
        """ Does the node have the given terminal base name? """
        return False

    def has_variant(self, s):
        """ Does the node have the given variant? """
        return False

    def child_has_nt_base(self, s):
        """ Does the node have a single child with the given nonterminal base name? """
        ch = self.child
        if ch is None:
            # No child
            return False
        if ch.nxt is not None:
            # More than one child
            return False
        return ch.has_nt_base(s)

    def children(self, test_f=None):
        """ Yield all children of this node (that pass a test function, if given) """
        c = self.child
        while c:
            if test_f is None or test_f(c):
                yield c
            c = c.nxt

    def first_child(self, test_f):
        """ Return the first child of this node that matches a test function, or None """
        c = self.child
        while c:
            if test_f(c):
                return c
            c = c.nxt
        return None

    def descendants(self, test_f=None):
        """ Do a depth-first traversal of all children of this node,
            returning those that pass a test function, if given """
        c = self.child
        while c:
            for cc in c.descendants():
                if test_f is None or test_f(cc):
                    yield cc
            if test_f is None or test_f(c):
                yield c
            c = c.nxt

    def contained_text(self):
        """ Return a string consisting of the literal text of all
            descendants of this node, in depth-first order """
        return NotImplementedError  # Should be overridden

    def string_self(self):
        """ String representation of the name of this node """
        raise NotImplementedError  # Should be overridden

    def string_rep(self, indent):
        """ Indented representation of this node """
        s = indent + self.string_self()
        if self.child is not None:
            s += " (\n" + self.child.string_rep(indent + "  ") + "\n" + indent + ")"
        if self.nxt is not None:
            s += ",\n" + self.nxt.string_rep(indent)
        return s

    def build_simple_tree(self, builder):
        """ Default action: recursively build the child nodes """
        for child in self.children():
            child.build_simple_tree(builder)

    def __str__(self):
        return self.string_rep("")

    def __repr__(self):
        return str(self)


class TerminalDescriptor:

    """ Wraps a terminal specification and is able to select a token meaning
        that matches that specification """

    _CASES = {"nf", "þf", "þgf", "ef"}
    _GENDERS = {"kk", "kvk", "hk"}
    _NUMBERS = {"et", "ft"}
    _PERSONS = {"p1", "p2", "p3"}

    def __init__(self, terminal):
        self.terminal = terminal
        self.is_literal = terminal[0] == '"'  # Literal terminal, i.e. "sem", "og"
        self.is_stem = terminal[0] == "'"  # Stem terminal, i.e. 'vera'_et_p3
        if self.is_literal or self.is_stem:
            # Go through hoops since it is conceivable that a
            # literal or stem may contain an underscore ('_')
            endq = terminal.rindex(terminal[0])
            elems = [terminal[0 : endq + 1]] + [
                v for v in terminal[endq + 1 :].split("_") if v
            ]
        else:
            elems = terminal.split("_")
        self.cat = elems[0]
        self.inferred_cat = self.cat
        if self.is_literal or self.is_stem:
            # In the case of a 'stem' or "literal",
            # check whether the category is specified
            # (e.g. 'halda:so'_et_p3)
            if ":" in self.cat:
                self.inferred_cat = self.cat.split(":")[-1][:-1]
        self.is_verb = self.inferred_cat == "so"
        self.varlist = elems[1:]
        self.variants = set(self.varlist)

        self.variant_vb = "vb" in self.variants
        self.variant_gr = "gr" in self.variants

        # BIN category set
        self.bin_cat = BIN_ORDFL.get(self.inferred_cat, None)

        # clean_terminal property cache
        self._clean_terminal = None

        # Gender of terminal
        self.gender = None
        gender = self.variants & self._GENDERS
        assert 0 <= len(gender) <= 1
        if gender:
            self.gender = next(iter(gender))

        # Case of terminal
        self.case = None
        if self.inferred_cat not in {"so", "fs"}:
            # We do not check cases for verbs, except so_lhþt ones
            case = self.variants & self._CASES
            assert 0 <= len(case) <= 1
            if case:
                self.case = next(iter(case))

        self.case_nf = self.case == "nf"

        # Person of terminal
        self.person = None
        person = self.variants & self._PERSONS
        assert 0 <= len(person) <= 1
        if person:
            self.person = next(iter(person))

        # Number of terminal
        self.number = None
        number = self.variants & self._NUMBERS
        assert 0 <= len(number) <= 1
        if number:
            self.number = next(iter(number))

    _OLD_BUGS = {
        "'margur'": "lo",
        "'fyrri'": "lo",
        "'seinni'": "lo",
        "'annar'": "fn",
        "'á fætur'": "ao",
        "'á_fætur'": "ao",
        "'né'": "st",
    }

    @property
    def clean_terminal(self):
        """ Return a 'clean' terminal name, having converted literals
            to a corresponding category, if available """
        if self._clean_terminal is None:
            if self.inferred_cat in self._GENDERS:
                # 'bróðir:kk'_gr_ft_nf becomes no_kk_gr_ft_nf
                self._clean_terminal = "no_" + self.inferred_cat
            elif self.inferred_cat in self._OLD_BUGS:
                # In older parses, we may have literal terminals
                # such as 'margur' that are not marked with a category
                self._clean_terminal = self._OLD_BUGS[self.inferred_cat]
            else:
                # 'halda:so'_et_p3 becomes so_et_p3
                self._clean_terminal = self.inferred_cat
            self._clean_terminal += "".join("_" + v for v in self.varlist)
        return self._clean_terminal

    def has_t_base(self, s):
        """ Does the node have the given terminal base name? """
        return self.cat == s

    def has_variant(self, s):
        """ Does the node have the given variant? """
        return s in self.variants

    def _bin_filter(self, m, case_override=None):
        """ Return True if the BIN meaning in m matches the variants for this terminal """
        if self.bin_cat is not None and m.ordfl not in self.bin_cat:
            return False
        if self.gender is not None:
            # Check gender match
            if self.inferred_cat == "pfn":
                # Personal pronouns don't have a gender in BÍN,
                # so don't disqualify on lack of gender
                pass
            elif self.inferred_cat == "no":
                if m.ordfl != self.gender:
                    return False
            elif self.gender.upper() not in m.beyging:
                return False
        if self.case is not None:
            # Check case match
            if case_override is not None:
                # Case override: we don't want other cases beside the given one
                for c in self._CASES:
                    if c != case_override:
                        if c.upper() in m.beyging:
                            return False
            elif self.case.upper() not in m.beyging:
                return False
        # Check number match
        if self.number is not None:
            if self.number.upper() not in m.beyging:
                return False

        if self.is_verb:
            # The following code is parallel to BIN_Token.verb_matches()
            for v in self.varlist:
                # Lookup variant to see if it is one of the required ones for verbs
                rq = BIN_Token._VERB_FORMS.get(v)
                if rq and rq not in m.beyging:
                    # If this is required variant that is not found in the form we have,
                    # return False
                    return False
            for v in ["sagnb", "lhþt", "bh"]:
                if BIN_Token.VARIANT[v] in m.beyging and v not in self.variants:
                    return False
            if "bh" in self.variants and "ST" in m.beyging:
                return False
            if self.varlist[0] not in "012":
                # No need for argument check: we're done, unless...
                if "lhþt" in self.variants:
                    # Special check for lhþt: may specify a case without it being an argument case
                    if any(
                        c in self.variants and BIN_Token.VARIANT[c] not in m.beyging
                        for c in BIN_Token.CASES
                    ):
                        # Terminal specified a non-argument case but the token doesn't have it:
                        # no match
                        return False
                return True
            nargs = int(self.varlist[0])
            if m.stofn in VerbObjects.VERBS[nargs]:
                if nargs == 0 or len(self.varlist) < 2:
                    # No arguments: we're done
                    return True
                for argspec in VerbObjects.VERBS[nargs][m.stofn]:
                    if all(self.varlist[1 + ix] == c for ix, c in enumerate(argspec)):
                        # This verb takes arguments that match the terminal
                        return True
                return False
            for i in range(0, nargs):
                if m.stofn in VerbObjects.VERBS[i]:
                    # This verb takes fewer arguments than the terminal requires, so no match
                    return False
            # Unknown verb: allow it to match
            return True

        # Check person match
        if self.person is not None:
            person = self.person.upper()
            person = person[1] + person[0]  # Turn p3 into 3P
            if person not in m.beyging:
                return False

        # Check VB/SB/MST for adjectives
        if "esb" in self.variants:
            if "ESB" not in m.beyging:
                return False
        if "evb" in self.variants:
            if "EVB" not in m.beyging:
                return False
        if "mst" in self.variants:
            if "MST" not in m.beyging:
                return False
        if self.variant_vb:
            if "VB" not in m.beyging:
                return False
        if "sb" in self.variants:
            if "SB" not in m.beyging:
                return False

        # Definite article
        if self.variant_gr:
            if "gr" not in m.beyging:
                return False
        return True

    def stem(self, bindb, word, at_start=False):
        """ Returns the stem of a word matching this terminal """
        if self.is_literal or self.is_stem:
            # A literal or stem terminal only matches a word if it has the given stem
            w = self.cat[1:-1]
            return w.split(":")[0]
        if " " in word:
            # Multi-word phrase: we return it unchanged
            return word
        _, meanings = bindb.lookup_word(word, at_start)
        if meanings:
            for m in meanings:
                if self._bin_filter(m):
                    # Found a matching meaning: return the stem
                    return m.stofn
        # No meanings found in BÍN: return the word itself as its own stem
        return word


def _root_lookup(text, at_start, terminal):
    """ Look up the root of a word that isn't found in the cache """
    with BIN_Db.get_db() as bin_db:
        w, m = bin_db.lookup_word(text, at_start)
    if m:
        # Find the meaning that matches the terminal
        td = TerminalNode._TD[terminal]
        m = next((x for x in m if td._bin_filter(x)), None)
    if m:
        if m.fl == "skst":
            # For abbreviations, return the original text as the
            # root (lemma), not the meaning of the abbreviation
            return text
        w = m.stofn
    return w.replace("-", "")


class TerminalNode(Node):

    """ A Node corresponding to a terminal """

    # Undeclinable terminal categories
    _NOT_DECLINABLE = frozenset(
        ["ao", "eo", "spao", "fs", "st", "stt", "nhm", "uh", "töl"]
    )
    _TD = dict()  # Cache of terminal descriptors

    # Cache of word roots (stems) keyed by (word, at_start, terminal)
    _root_cache = LRU_Cache(_root_lookup, maxsize=16384)

    def __init__(self, terminal, augmented_terminal, token, tokentype, aux, at_start):
        super().__init__()
        td = self._TD.get(terminal)
        if td is None:
            # Not found in cache: make a new one
            td = TerminalDescriptor(terminal)
            self._TD[terminal] = td
        self.td = td
        self.token = token
        self.text = token[1:-1]  # Cut off quotes
        self.at_start = at_start
        self.tokentype = tokentype
        self.is_word = tokentype in {"WORD", "PERSON"}
        self.is_literal = td.is_literal
        self.is_declinable = (not self.is_literal) and (
            td.inferred_cat not in self._NOT_DECLINABLE
        )
        self.augmented_terminal = augmented_terminal
        self.aux = aux  # Auxiliary information, originally from token.t2
        # Cache the root form of this word so that it is only looked up
        # once, even if multiple processors scan this tree
        self.root_cache = None
        self.nominative_cache = None
        self.indefinite_cache = None
        self.canonical_cache = None

    @property
    def cat(self):
        return self.td.inferred_cat

    def has_t_base(self, s):
        """ Does the node have the given terminal base name? """
        return self.td.has_t_base(s)

    def has_variant(self, s):
        """ Does the node have the given variant? """
        return self.td.has_variant(s)

    def contained_text(self):
        """ Return a string consisting of the literal text of all
            descendants of this node, in depth-first order """
        return self.text

    def _root(self, bin_db):
        """ Look up the root of the word associated with this terminal """
        # Lookup the token in the BIN database
        if (not self.is_word) or self.is_literal:
            return self.text
        return self._root_cache(self.text, self.at_start, self.td.terminal)

    def _lazy_eval_root(self):
        """ Return a word root (stem) function object, with arguments, that can be
            used for lazy evaluation of word stems. """
        if (not self.is_word) or self.is_literal:
            return self.text
        return self._root_cache, (self.text, self.at_start, self.td.terminal)

    def lookup_alternative(self, bin_db, replace_func, sort_func=None):
        """ Return a different (but always nominative case) word form, if available,
            by altering the beyging spec via the given replace_func function """
        w, m = bin_db.lookup_word(self.text, self.at_start)
        if m:
            # Narrow the meanings down to those that are compatible with the terminal
            m = [x for x in m if self.td._bin_filter(x)]
        if m:
            # Look up the distinct roots of the word
            result = []
            for x in m:

                # Calculate a new 'beyging' string with the nominative case
                beyging = replace_func(x.beyging)

                if beyging is x.beyging:
                    # No replacement made: word form is identical in the nominative case
                    result.append(x)
                else:
                    # Lookup the same word (identified by 'utg') but a different declination
                    parts = x.ordmynd.split("-")
                    stofn = x.stofn.split("-")[-1] if len(parts) > 1 else x.stofn
                    prefix = "".join(parts[0:-1]) if len(parts) > 1 else ""
                    # Go through all nominative forms of this word form until we
                    # find one that matches the meaning ('beyging') that we're
                    # looking for. It also must be the same word category and
                    # the same stem and identifier ('utg'). In fact the 'utg' check
                    # alone should be sufficient, but better safe than sorry.
                    n = bin_db.lookup_nominative(parts[-1])  # Note: this call is cached
                    r = [
                        nm
                        for nm in n
                        if nm.stofn == stofn
                        and nm.ordfl == x.ordfl
                        and nm.utg == x.utg
                        and nm.beyging == beyging
                    ]
                    if prefix:
                        # Add the word prefix again in front, if any
                        result += bin_db.prefix_meanings(r, prefix)
                    else:
                        result += r
            if result:
                if len(result) > 1 and sort_func is not None:
                    # Sort the result before choosing the matching meaning
                    result.sort(key=sort_func)
                # There can be more than one word form that matches our spec.
                # We can't choose between them so we simply return the first one.
                w = result[0].ordmynd
        return w.replace("-", "")

    def _nominative(self, bin_db):
        """ Look up the nominative form of the word associated with this terminal """
        # Lookup the token in the BIN database
        if (not self.is_word) or self.td.case_nf or not self.is_declinable:
            # Not a word, already nominative or not declinable: return it as-is
            return self.text
        if not self.text:
            # print("self.text is empty, token is {0}, terminal is {1}".format(self.token, self.td.terminal))
            assert False

        def replace_beyging(b, by_case="NF"):
            """ Change a beyging string to specify a different case """
            for case in ("NF", "ÞF", "ÞGF", "EF"):
                if case != by_case and case in b:
                    return b.replace(case, by_case)
            return b

        def sort_by_gr(m):
            """ Sort meanings having a definite article (greinir) after those that do not """
            return 1 if "gr" in m.beyging else 0

        # If this terminal doesn't have a 'gr' variant, prefer meanings in nominative
        # case that do not include 'gr'
        sort_func = None if self.has_variant("gr") else sort_by_gr

        # Lookup the same word stem but in the nominative case
        w = self.lookup_alternative(bin_db, replace_beyging, sort_func=sort_func)

        if self.text.isupper():
            # Original word was all upper case: convert result to upper case
            w = w.upper()
        elif self.text[0].isupper():
            # First letter was upper case: convert result accordingly
            w = w[0].upper() + w[1:]
        return w

    def _indefinite(self, bin_db):
        """ Look up the indefinite nominative form of a noun or adjective associated with this terminal """
        # Lookup the token in the BIN database
        if (not self.is_word) or self.is_literal:
            # Not a word, not a noun or already indefinite: return it as-is
            return self.text
        if self.cat not in {"no", "lo"}:
            return self.text
        if self.td.case_nf and (
            (self.cat == "no" and not self.td.variant_gr)
            or (self.cat == "lo" and not self.td.variant_vb)
        ):
            # Already in nominative case, and indefinite in the case of a noun
            # or strong declination in the case of an adjective
            return self.text

        if not self.text:
            # print("self.text is empty, token is {0}, terminal is {1}".format(self.token, self.td.terminal))
            assert False

        def replace_beyging(b, by_case="NF"):
            """ Change a beyging string to specify a different case, without the definitive article """
            for case in ("NF", "ÞF", "ÞGF", "EF"):
                if case != by_case and case in b:
                    return (
                        b.replace(case, by_case).replace("gr", "").replace("VB", "SB")
                    )
            # No case found: shouldn't really happen, but whatever
            return b.replace("gr", "").replace("VB", "SB")

        # Lookup the same word stem but in the nominative case
        w = self.lookup_alternative(bin_db, replace_beyging)
        return w

    def _canonical(self, bin_db):
        """ Look up the singular indefinite nominative form of a noun or adjective associated with this terminal """
        # Lookup the token in the BIN database
        if (not self.is_word) or self.is_literal:
            # Not a word, not a noun or already indefinite: return it as-is
            return self.text
        if self.cat not in {"no", "lo"}:
            return self.text
        if (
            self.td.case_nf
            and self.td.number == "et"
            and (
                (self.cat == "no" and not self.td.variant_gr)
                or (self.cat == "lo" and not self.td.variant_vb)
            )
        ):
            # Already singular, nominative, indefinite (if noun)
            return self.text

        if not self.text:
            # print("self.text is empty, token is {0}, terminal is {1}".format(self.token, self.terminal))
            assert False

        def replace_beyging(b, by_case="NF"):
            """ Change a 'beyging' string to specify a different case, without the definitive article """
            for case in ("NF", "ÞF", "ÞGF", "EF"):
                if case != by_case and case in b:
                    return (
                        b.replace(case, by_case)
                        .replace("FT", "ET")
                        .replace("gr", "")
                        .replace("VB", "SB")
                    )
            # No case found: shouldn't really happen, but whatever
            return b.replace("FT", "ET").replace("gr", "").replace("VB", "SB")

        # Lookup the same word stem but in the nominative case
        w = self.lookup_alternative(bin_db, replace_beyging)
        return w

    def root(self, state, params):
        """ Calculate the root form (stem) of this node's text """
        if self.root_cache is None:
            # Not already cached: look up in database
            bin_db = state["bin_db"]
            self.root_cache = self._root(bin_db)
        return self.root_cache

    def nominative(self, state, params):
        """ Calculate the nominative form of this node's text """
        if self.nominative_cache is None:
            # Not already cached: look up in database
            bin_db = state["bin_db"]
            self.nominative_cache = self._nominative(bin_db)
        return self.nominative_cache

    def indefinite(self, state, params):
        """ Calculate the nominative, indefinite form of this node's text """
        if self.indefinite_cache is None:
            # Not already cached: look up in database
            bin_db = state["bin_db"]
            self.indefinite_cache = self._indefinite(bin_db)
        return self.indefinite_cache

    def canonical(self, state, params):
        """ Calculate the singular, nominative, indefinite form of this node's text """
        if self.canonical_cache is None:
            # Not already cached: look up in database
            bin_db = state["bin_db"]
            self.canonical_cache = self._canonical(bin_db)
        return self.canonical_cache

    def string_self(self):
        return self.td.terminal + " <" + self.token + ">"

    def process(self, state, params):
        """ Prepare a result object to be passed up to enclosing nonterminals """
        assert not params  # A terminal node should not have parameters
        result = Result(self, state, None)  # No params
        result._terminal = self.td.terminal
        result._text = self.text
        result._token = self.token
        result._tokentype = self.tokentype
        return result

    def build_simple_tree(self, builder):
        """ Create a terminal node in a simple tree for this TerminalNode """
        d = dict(x=self.text, k=self.tokentype)
        if self.tokentype != "PUNCTUATION":
            # Terminal
            d["t"] = t = self.td.clean_terminal
            a = self.augmented_terminal
            if a and a != t:
                # We have an augmented terminal and it's different from the
                # pure grammar terminal: store it
                d["a"] = a
            else:
                d["a"] = t
            if t[0] == '"' or t[0] == "'":
                assert False, (
                    "Wrong terminal: {0}, text is '{1}', token {2}, tokentype {3}"
                    .format(self.td.terminal, self.text, self.token, self.tokentype)
                )
            # Category
            d["c"] = self.cat
            if self.tokentype == "WORD":
                # Stem: Don't evaluate it right away, because we may never
                # need it, and the lookup is expensive. Instead, return a
                # tuple that will be used later to look up the stem if and
                # when needed.
                d["s"] = self._lazy_eval_root()
                # !!! f and b fields missing
        builder.push_terminal(d)


class PersonNode(TerminalNode):

    """ Specialized TerminalNode for person terminals """

    def __init__(self, terminal, augmented_terminal, token, tokentype, aux, at_start):
        super().__init__(terminal, augmented_terminal, token, tokentype, aux, at_start)
        # Load the full names from the auxiliary JSON information
        gender = self.td.gender or None
        case = self.td.case or None
        fn_list = json.loads(aux) if aux else []  # List of tuples: (name, gender, case)
        # Collect the potential full names that are available in nominative
        # case and match the gender of the terminal
        self.fullnames = [
            fn
            for fn, g, c in fn_list
            if (gender is None or g == gender) and (case is None or c == case)
        ]

    def _root(self, bin_db):
        """ Calculate the root (canonical) form of this person name """
        # If we already have a full name coming from the tokenizer, use it
        # (full name meaning that it includes the patronym/matronym even
        # if it was not present in the original token)
        # Start by checking whether we already have a matching full name,
        # i.e. one in nominative case and with the correct gender
        if self.fullnames:
            # We may have more than one matching full name, but we have no means
            # of knowing which one is correct, so we simply return the first one
            return self.fullnames[0]
        gender = self.td.gender
        case = self.td.case.upper()
        # Lookup the token in the BIN database
        # Look up each part of the name
        at_start = self.at_start
        name = []
        for part in self.text.split(" "):
            w, m = bin_db.lookup_word(part, at_start)
            at_start = False
            if m:
                m = [
                    x
                    for x in m
                    if x.ordfl == gender and case in x.beyging and "ET" in x.beyging
                    # Do not accept 'Sigmund' as a valid stem for word forms that
                    # are identical with the stem 'Sigmundur'
                    and (
                        x.stofn not in DisallowedNames.STEMS
                        or self.td.case not in DisallowedNames.STEMS[x.stofn]
                    )
                ]
            if m:
                w = m[0].stofn
            name.append(w.replace("-", ""))
        return " ".join(name)

    def _nominative(self, bin_db):
        """ The nominative is identical to the root """
        return self._root(bin_db)

    def _indefinite(self, bin_db):
        """ The indefinite is identical to the nominative """
        return self._nominative(bin_db)

    def _canonical(self, bin_db):
        """ The canonical is identical to the nominative """
        return self._nominative(bin_db)

    def build_simple_tree(self, builder):
        """ Create a terminal node in a simple tree for this PersonNode """
        d = dict(x=self.text, k=self.tokentype)
        # Category = gender
        d["c"] = self.td.gender or self.td.cat
        # Stem
        d["s"] = self.root(builder.state, None)
        # Terminal
        d["t"] = self.td.terminal
        builder.push_terminal(d)


class NonterminalNode(Node):

    """ A Node corresponding to a nonterminal """

    def __init__(self, nonterminal):
        super().__init__()
        self.nt = nonterminal
        elems = nonterminal.split("_")
        # Calculate the base name of this nonterminal (without variants)
        self.nt_base = elems[0]
        self.variants = set(elems[1:])
        self.is_repeated = self.nt_base[-1] in _REPEAT_SUFFIXES

    def build_simple_tree(self, builder):
        builder.push_nonterminal(self.nt_base)
        # This builds the child nodes
        super().build_simple_tree(builder)
        builder.pop_nonterminal()

    @property
    def text(self):
        """ A nonterminal node has no text of its own """
        return ""

    def contained_text(self):
        """ Return a string consisting of the literal text of all
            descendants of this node, in depth-first order """
        return " ".join(d.text for d in self.descendants() if d.text)

    def has_nt_base(self, s):
        """ Does the node have the given nonterminal base name? """
        return self.nt_base == s

    def has_variant(self, s):
        """ Does the node have the given variant? """
        return s in self.variants

    def string_self(self):
        return self.nt

    def root(self, state, params):
        """ The root form of a nonterminal is a sequence of the root forms of its children (parameters) """
        return " ".join(p._root for p in params if p is not None and p._root)

    def nominative(self, state, params):
        """ The nominative form of a nonterminal is a sequence of the nominative forms of its children (parameters) """
        return " ".join(
            p._nominative for p in params if p is not None and p._nominative
        )

    def indefinite(self, state, params):
        """ The indefinite form of a nonterminal is a sequence of the indefinite forms of its children (parameters) """
        return " ".join(
            p._indefinite for p in params if p is not None and p._indefinite
        )

    def canonical(self, state, params):
        """ The canonical form of a nonterminal is a sequence of the canonical forms of its children (parameters) """
        return " ".join(p._canonical for p in params if p is not None and p._canonical)

    def process(self, state, params):
        """ Apply any requested processing to this node """
        result = Result(self, state, params)
        result._nonterminal = self.nt
        # Calculate the combined text rep of the results of the children
        result._text = " ".join(p._text for p in params if p is not None and p._text)
        for p in params:
            # Copy all user variables (attributes not starting with an underscore _)
            # coming from the children into the result
            if p is not None:
                result.copy_from(p)
        # Invoke a processor function for this nonterminal, if
        # present in the given processor module
        if params and not self.is_repeated:
            # Don't invoke if this is an epsilon nonterminal (i.e. has no children)
            processor = state["processor"]
            func = (
                getattr(processor, self.nt_base, state["_default"])
                if processor
                else None
            )
            if func is not None:
                try:
                    func(self, params, result)
                except TypeError as ex:
                    print("Attempt to call {0}() in processor raised exception {1}"
                        .format(func.__qualname__, ex)
                    )
                    raise
        return result


class TreeBase:

    """ A tree corresponding to a single parsed article """

    # A map of terminal types to node constructors
    _TC = {"person": PersonNode}

    def __init__(self):
        self.s = OrderedDict()  # Sentence dictionary
        self.scores = dict()  # Sentence scores
        self.lengths = dict()  # Sentence lengths, in tokens
        self.stack = None
        self.n = None  # Index of current sentence
        self.at_start = False  # First token of sentence?

    def __getitem__(self, n):
        """ Allow indexing to get sentence roots from the tree """
        return self.s[n]

    def __contains__(self, n):
        """ Allow query of sentence indices """
        return n in self.s

    def sentences(self):
        """ Enumerate the sentences in this tree """
        for ix, sent in self.s.items():
            yield ix, sent

    def score(self, n):
        """ Return the score of the sentence with index n, or 0 if unknown """
        return self.scores.get(n, 0)

    def length(self, n):
        """ Return the length of the sentence with index n, in tokens, or 0 if unknown """
        return self.lengths.get(n, 0)

    def simple_trees(self, nt_map=None, id_map=None, terminal_map=None):
        """ Generate simple trees out of the sentences in this tree """
        # Hack to allow nodes to access the BIN database
        with BIN_Db.get_db() as bin_db:
            state = dict(bin_db=bin_db)
            for ix, sent in self.s.items():
                builder = SimpleTreeBuilder(nt_map, id_map, terminal_map)
                builder.state = state
                sent.build_simple_tree(builder)
                yield ix, builder.tree

    def push(self, n, node):
        """ Add a node into the tree at the right level """
        if n == len(self.stack):
            # First child of parent
            if n:
                self.stack[n - 1].set_child(node)
            self.stack.append(node)
        else:
            assert n < len(self.stack)
            # Next child of parent
            self.stack[n].set_next(node)
            self.stack[n] = node
            if n + 1 < len(self.stack):
                self.stack = self.stack[0 : n + 1]

    def handle_R(self, n):
        """ Reynir version info """
        pass

    def handle_C(self, n):
        """ Sentence score """
        assert self.n is not None
        assert self.n not in self.scores
        self.scores[self.n] = n

    def handle_L(self, n):
        """ Sentence length """
        assert self.n is not None
        assert self.n not in self.lengths
        self.lengths[self.n] = n

    def handle_S(self, n):
        """ Start of sentence """
        self.n = n
        self.stack = []
        self.at_start = True

    def handle_Q(self, n):
        """ End of sentence """
        # Store the root of the sentence tree at the appropriate index
        # in the dictionary
        assert self.n is not None
        assert self.n not in self.s
        self.s[self.n] = self.stack[0]
        self.stack = None
        self.n = None

    def handle_E(self, n):
        """ End of sentence with error """
        # Nothing stored
        assert self.n not in self.s
        self.stack = None
        self.n = None

    def handle_P(self, n):
        """ Epsilon node: leave the parent nonterminal childless """
        pass

    @staticmethod
    def _parse_T(s):
        """ Parse a T (Terminal) descriptor """
        # The string s contains:
        # terminal "token" [TOKENTYPE] [auxiliary-json]

        # The terminal may itself be a single- or double-quoted string,
        # in which case it may contain underscores, colons and other
        # punctuation. It can then be followed by variant names,
        # separated by underscores. The \w regexp pattern matches
        # alpabetic characters as well as digits and underscores.
        if s[0] == "'":
            r = re.match(r"\'[^\']*\'\w*", s)
            terminal = r.group() if r else ""
            s = s[r.end() + 1 :] if r else ""
        elif s[0] == '"':
            r = re.match(r"\"[^\"]*\"\w*", s)
            terminal = r.group() if r else ""
            s = s[r.end() + 1 :] if r else ""
        else:
            a = s.split(" ", maxsplit=1)
            terminal = a[0]
            s = a[1]
        # Retrieve token text
        r = re.match(r"\"[^\"]*\"", s)
        if r is None:
            # Compatibility: older versions used single quotes around token text
            r = re.match(r"\'[^\']*\'", s)
        token = r.group() if r else ""
        s = s[r.end() + 1 :] if r else ""
        augmented_terminal = terminal
        if s:
            a = s.split(" ", maxsplit=1)
            tokentype = a[0]
            if tokentype[0].islower():
                # The following string is actually an augmented terminal,
                # corresponding to a word token
                augmented_terminal = tokentype
                tokentype = "WORD"
                aux = ""
            else:
                aux = a[1] if len(a) > 1 else ""  # Auxiliary info (originally token.t2)
        else:
            # Default token type
            tokentype = "WORD"
            aux = ""
        # The 'cat' extracted here is actually the first part of the terminal
        # name, which is not the word category in all cases (for instance not
        # for literal terminals).
        cat = terminal.split("_", maxsplit=1)[0]
        return (terminal, augmented_terminal, token, tokentype, aux, cat)

    def handle_T(self, n, s):
        """ Terminal """
        terminal, augmented_terminal, token, tokentype, aux, cat = self._parse_T(s)
        constructor = self._TC.get(cat, TerminalNode)
        self.push(
            n,
            constructor(
                terminal, augmented_terminal, token, tokentype, aux, self.at_start
            ),
        )
        self.at_start = False

    def handle_N(self, n, nonterminal):
        """ Nonterminal """
        self.push(n, NonterminalNode(nonterminal))

    def load(self, txt):
        """ Loads a tree from the text format stored by the scraper """
        for line in txt.split("\n"):
            if not line:
                continue
            a = line.split(" ", maxsplit=1)
            if not a:
                continue
            code = a[0]
            n = int(code[1:])
            f = getattr(self, "handle_" + code[0], None)
            if f:
                if len(a) >= 2:
                    f(n, a[1])
                else:
                    f(n)
            else:
                assert False, "*** No handler for {0}".format(line)


class Tree(TreeBase):

    """ A processable tree corresponding to a single parsed article """

    def __init__(self, url="", authority=1.0):
        super().__init__()
        self.url = url
        self.authority = authority

    def visit_children(self, state, node):
        """ Visit the children of node, obtain results from them and pass them to the node """
        # First check whether the processor has a visit() method
        visit = state["_visit"]
        if visit is not None and not visit(state, node):
            # Call the visit() method and if it returns False, we do not visit this node
            # or its children
            return None
        return node.process(
            state, [self.visit_children(state, child) for child in node.children()]
        )

    def process_sentence(self, state, tree):
        """ Process a single sentence tree """
        assert tree.nxt is None
        result = self.visit_children(state, tree)
        # Sentence processing completed:
        # Invoke a function called 'sentence(state, result)',
        # if present in the processor
        sentence = state["_sentence"]
        if sentence is not None:
            sentence(state, result)

    def process(self, session, processor, **kwargs):
        """ Process a tree for an entire article """
        # For each sentence in turn, do a depth-first traversal,
        # visiting each parent node after visiting its children
        # Initialize the running state that we keep between sentences

        article_begin = getattr(processor, "article_begin", None) if processor else None
        article_end = getattr(processor, "article_end", None) if processor else None
        sentence = getattr(processor, "sentence", None) if processor else None
        # If visit(state, node) returns False for a node, do not visit child nodes
        visit = getattr(processor, "visit", None) if processor else None
        # If no handler exists for a nonterminal, call default() instead
        default = getattr(processor, "default", None) if processor else None

        with BIN_Db.get_db() as bin_db:

            state = {
                "session": session,
                "processor": processor,
                "bin_db": bin_db,
                "url": self.url,
                "authority": self.authority,
                "_sentence": sentence,
                "_visit": visit,
                "_default": default,
                "index": 0,
            }
            # Add state parameters passed via keyword arguments, if any
            state.update(kwargs)

            # Call the article_begin(state) function, if it exists
            if article_begin is not None:
                article_begin(state)
            # Process the (parsed) sentences in the article
            for index, tree in self.s.items():
                state["index"] = index
                self.process_sentence(state, tree)
            # Call the article_end(state) function, if it exists
            if article_end is not None:
                article_end(state)


class TreeGist(TreeBase):

    """ A gist of a tree corresponding to a single parsed article.
        A gist simply knows which sentences are present in the tree
        and what the error token index is for sentences that are not present. """

    def __init__(self):
        super().__init__()
        # Dictionary of error token indices for sentences that weren't successfully parsed
        self._err_index = dict()

    def err_index(self, n):
        """ Return the error token index for an unparsed sentence, if any, or None """
        return self._err_index.get(n)

    def push(self, n, node):
        """ This should not be invoked for a gist """
        assert False

    def handle_Q(self, n):
        """ End of sentence """
        # Simply note that the sentence is present without storing it
        assert self.n is not None
        assert self.n not in self.s
        self.s[self.n] = None
        self.stack = None
        self.n = None

    def handle_E(self, n):
        """ End of sentence with error """
        super().handle_E(n)
        self._err_index[self.n] = n  # Note the index of the error token

    def handle_T(self, n, s):
        """ Terminal """
        # No need to store anything for gists
        pass

    def handle_N(self, n, nonterminal):
        """ Nonterminal """
        # No need to store anything for gists
        pass


TreeToken = namedtuple(
    "TreeToken", ["terminal", "augmented_terminal", "token", "tokentype", "aux", "cat"]
)


class TreeTokenList(TreeBase):

    """ A tree that allows easy iteration of its token/terminal matches """

    def __init__(self):
        super().__init__()

    def handle_Q(self, n):
        """ End of sentence """
        assert self.n is not None
        assert self.n not in self.s
        self.s[self.n] = self.stack
        self.stack = None
        self.n = None

    def handle_T(self, n, s):
        """ Terminal """
        t = self._parse_T(s)
        # Append to token list for current sentence
        assert self.stack is not None
        self.stack.append(TreeToken(*t))

    def handle_N(self, n, nonterminal):
        """ Nonterminal """
        # No action required for token lists
        pass
