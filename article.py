"""

    Reynir: Natural language processing for Icelandic

    Article class

    Copyright (C) 2018 Miðeind ehf.
    Author: Vilhjálmur Þorsteinsson

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


    This module contains a class modeling an article originating
    from a scraped web page.

"""

import json
import uuid
from datetime import datetime
from collections import OrderedDict, defaultdict

from settings import NoIndexWords
from scraperdb import Article as ArticleRow, SessionContext, Word, Root, DataError, desc
from fetcher import Fetcher
from tokenizer import TOK, tokenize
from reynir.fastparser import Fast_Parser, ParseError, ParseForestDumper
from incparser import IncrementalParser
from tree import Tree
from treeutil import TreeUtility


# We don't bother parsing sentences that have more tokens than 100,
# since they require lots of memory (>16 GB) and may take
# minutes to parse
MAX_SENTENCE_TOKENS = 100


class Article:

    """ An Article represents a new article typically scraped from a web site,
        as it is tokenized, parsed and stored in the Reynir database. """

    _parser = None

    @classmethod
    def _init_class(cls):
        """ Initialize class attributes """
        if cls._parser is None:
            cls._parser = Fast_Parser(verbose=False)  # Don't emit diagnostic messages

    @classmethod
    def cleanup(cls):
        if cls._parser is not None:
            cls._parser.cleanup()
            cls._parser = None

    @classmethod
    def get_parser(cls):
        if cls._parser is None:
            cls._init_class()
        return cls._parser

    @classmethod
    def reload_parser(cls):
        """ Force reload of a fresh parser instance """
        cls._parser = None
        cls._init_class()

    @classmethod
    def parser_version(cls):
        """ Return the current grammar timestamp + parser version """
        cls._init_class()
        return cls._parser.version

    def __init__(self, uuid=None, url=None):
        self._uuid = uuid
        self._url = url
        self._heading = ""
        self._author = ""
        self._timestamp = datetime.utcnow()
        self._authority = 1.0
        self._scraped = None
        self._parsed = None
        self._processed = None
        self._indexed = None
        self._scr_module = None
        self._scr_class = None
        self._scr_version = None
        self._parser_version = None
        self._num_tokens = None
        self._num_sentences = 0
        self._num_parsed = 0
        self._ambiguity = 1.0
        self._html = None
        self._tree = None
        self._root_id = None
        self._root_domain = None
        self._helper = None
        self._tokens = None  # JSON string
        self._raw_tokens = None  # The tokens themselves
        self._words = None  # The individual word stems, in a dictionary

    @classmethod
    def _init_from_row(cls, ar):
        """ Initialize a fresh Article instance from a database row object """
        a = cls(uuid=ar.id)
        a._url = ar.url
        a._heading = ar.heading
        a._author = ar.author
        a._timestamp = ar.timestamp
        a._authority = ar.authority
        a._scraped = ar.scraped
        a._parsed = ar.parsed
        a._processed = ar.processed
        a._indexed = ar.indexed
        a._scr_module = ar.scr_module
        a._scr_class = ar.scr_class
        a._scr_version = ar.scr_version
        a._parser_version = ar.parser_version
        assert a._num_tokens is None
        a._num_sentences = ar.num_sentences
        a._num_parsed = ar.num_parsed
        a._ambiguity = ar.ambiguity
        a._html = ar.html
        a._tree = ar.tree
        a._tokens = ar.tokens
        assert a._raw_tokens is None
        a._root_id = ar.root_id
        a._root_domain = ar.root.domain if ar.root else None
        return a

    @classmethod
    def _init_from_scrape(cls, url, enclosing_session=None):
        """ Scrape an article from its URL """
        if url is None:
            return None
        a = cls(url=url)
        with SessionContext(enclosing_session) as session:
            # Obtain a helper corresponding to the URL
            html, metadata, helper = Fetcher.fetch_url_html(url, session)
            if html is None:
                return a
            a._html = html
            if metadata is not None:
                a._heading = metadata.heading
                a._author = metadata.author
                a._timestamp = metadata.timestamp
                a._authority = metadata.authority
            a._scraped = datetime.utcnow()
            if helper is not None:
                a._scr_module = helper.scr_module
                a._scr_class = helper.scr_class
                a._scr_version = helper.scr_version
                a._root_id = helper.root_id
                a._root_domain = helper.domain
            return a

    @classmethod
    def load_from_url(cls, url, enclosing_session=None):
        """ Load or scrape an article, given its URL """
        with SessionContext(enclosing_session) as session:
            ar = session.query(ArticleRow).filter(ArticleRow.url == url).one_or_none()
            if ar is not None:
                return cls._init_from_row(ar)
            # Not found in database: attempt to fetch
            return cls._init_from_scrape(url, session)

    @classmethod
    def scrape_from_url(cls, url, enclosing_session=None):
        """ Force fetch of an article, given its URL """
        with SessionContext(enclosing_session) as session:
            ar = session.query(ArticleRow).filter(ArticleRow.url == url).one_or_none()
            a = cls._init_from_scrape(url, session)
            if a is not None and ar is not None:
                # This article already existed in the database, so note its UUID
                a._uuid = ar.id
            return a

    @classmethod
    def load_from_uuid(cls, uuid, enclosing_session=None):
        """ Load an article, given its UUID """
        with SessionContext(enclosing_session) as session:
            try:
                ar = (
                    session
                    .query(ArticleRow)
                    .filter(ArticleRow.id == uuid)
                    .one_or_none()
                )
            except DataError:
                # Probably wrong UUID format
                ar = None
            return None if ar is None else cls._init_from_row(ar)

    def person_names(self):
        """ A generator yielding all person names in an article token stream """
        if self._raw_tokens is None and self._tokens:
            # Lazy generation of the raw tokens from the JSON rep
            self._raw_tokens = json.loads(self._tokens)
        if self._raw_tokens:
            for p in self._raw_tokens:
                for sent in p:
                    for t in sent:
                        if t.get("k") == TOK.PERSON:
                            # The full name of the person is in the v field
                            yield t["v"]

    def entity_names(self):
        """ A generator for entity names from an article token stream """
        if self._raw_tokens is None and self._tokens:
            # Lazy generation of the raw tokens from the JSON rep
            self._raw_tokens = json.loads(self._tokens)
        if self._raw_tokens:
            for p in self._raw_tokens:
                for sent in p:
                    for t in sent:
                        if t.get("k") == TOK.ENTITY:
                            # The entity name
                            yield t["x"]

    def create_register(self, session, all_names=False):
        """ Create a name register dictionary for this article """
        register = {}
        from query import add_name_to_register, add_entity_to_register

        for name in self.person_names():
            add_name_to_register(name, register, session, all_names=all_names)
        # Add register of entity names
        for name in self.entity_names():
            add_entity_to_register(name, register, session, all_names=all_names)
        return register

    def _store_words(self, session):
        """ Store word stems """
        assert session is not None
        # Delete previously stored words for this article
        session.execute(Word.table().delete().where(Word.article_id == self._uuid))
        # Index the words by storing them in the words table
        for word, cnt in self._words.items():
            if word.cat not in NoIndexWords.CATEGORIES_TO_INDEX:
                # We do not index closed word categories and non-distinctive constructs
                continue
            if (word.stem, word.cat) in NoIndexWords.SET:
                # Specifically excluded from indexing in Reynir.conf (Main.conf)
                continue
            if len(word.stem) > Word.MAX_WORD_LEN:
                # Shield the database from too long words
                continue
            # Interesting word: let's index it
            w = Word(article_id=self._uuid, stem=word.stem, cat=word.cat, cnt=cnt)
            session.add(w)
        # Offload the new data from Python to PostgreSQL
        session.flush()

    def _parse(self, enclosing_session=None, verbose=False):
        """ Parse the article content to yield parse trees and annotated token list """
        with SessionContext(enclosing_session) as session:

            # Convert the content soup to a token iterable (generator)
            toklist = Fetcher.tokenize_html(self._url, self._html, session)

            bp = self.get_parser()
            ip = IncrementalParser(bp, toklist, verbose=verbose)

            # List of paragraphs containing a list of sentences containing token lists
            # for sentences in string dump format (1-based paragraph and sentence indices)
            pgs = []

            # Dict of parse trees in string dump format,
            # stored by sentence index (1-based)
            trees = OrderedDict()

            # Word stem dictionary, indexed by (stem, cat)
            words = defaultdict(int)
            num_sent = 0

            for p in ip.paragraphs():

                pgs.append([])

                for sent in p.sentences():

                    num_sent += 1
                    num_tokens = len(sent)

                    # We don't attempt to parse very long sentences (>100 tokens)
                    # since they are memory intensive (>16 GB) and may take
                    # minutest to process
                    if num_tokens <= MAX_SENTENCE_TOKENS and sent.parse():
                        # Obtain a text representation of the parse tree
                        token_dicts = TreeUtility.dump_tokens(
                            sent.tokens, sent.tree, words
                        )
                        # Create a verbose text representation of
                        # the highest scoring parse tree
                        tree = ParseForestDumper.dump_forest(
                            sent.tree, token_dicts=token_dicts
                        )
                        # Add information about the sentence tree's score
                        # and the number of tokens
                        trees[num_sent] = "\n".join(
                            [
                                "C{0}".format(sent.score),
                                "L{0}".format(num_tokens),
                                tree
                            ]
                        )
                    else:
                        # Error, sentence too long or no parse:
                        # add an error index entry for this sentence
                        if num_tokens > MAX_SENTENCE_TOKENS:
                            # Set the error index at the first
                            # token outside the maximum limit
                            eix = MAX_SENTENCE_TOKENS
                        else:
                            eix = sent.err_index
                        token_dicts = TreeUtility.dump_tokens(
                            sent.tokens, None, None, eix
                        )
                        trees[num_sent] = "E{0}".format(eix)

                    pgs[-1].append(token_dicts)

            # parse_time = ip.parse_time

            self._parsed = datetime.utcnow()
            self._parser_version = bp.version
            self._num_tokens = ip.num_tokens
            self._num_sentences = ip.num_sentences
            self._num_parsed = ip.num_parsed
            self._ambiguity = ip.ambiguity

            # Make one big JSON string for the paragraphs, sentences and tokens
            self._raw_tokens = pgs
            self._tokens = json.dumps(pgs, separators=(",", ":"), ensure_ascii=False)

            # Keep the bag of words (stem, category, count for each word)
            self._words = words

            # Create a tree representation string out of all the accumulated parse trees
            self._tree = "".join(
                "S{0}\n{1}\n".format(key, val) for key, val in trees.items()
            )

    def store(self, enclosing_session=None):
        """ Store an article in the database, inserting it or updating """
        with SessionContext(enclosing_session, commit=True) as session:
            if self._uuid is None:
                # Insert a new row
                self._uuid = str(uuid.uuid1())
                ar = ArticleRow(
                    id=self._uuid,
                    url=self._url,
                    root_id=self._root_id,
                    heading=self._heading,
                    author=self._author,
                    timestamp=self._timestamp,
                    authority=self._authority,
                    scraped=self._scraped,
                    parsed=self._parsed,
                    processed=self._processed,
                    indexed=self._indexed,
                    scr_module=self._scr_module,
                    scr_class=self._scr_class,
                    scr_version=self._scr_version,
                    parser_version=self._parser_version,
                    num_sentences=self._num_sentences,
                    num_parsed=self._num_parsed,
                    ambiguity=self._ambiguity,
                    html=self._html,
                    tree=self._tree,
                    tokens=self._tokens,
                )
                session.add(ar)
                if self._words:
                    # Store the word stems occurring in the article
                    self._store_words(session)
                return True

            # Update an already existing row by UUID
            ar = (
                session
                .query(ArticleRow)
                .filter(ArticleRow.id == self._uuid)
                .one_or_none()
            )
            if ar is None:
                # UUID not found: something is wrong here...
                return False

            # Update the columns
            # UUID is immutable
            ar.url = self._url
            ar.root_id = self._root_id
            ar.heading = self._heading
            ar.author = self._author
            ar.timestamp = self._timestamp
            ar.authority = self._authority
            ar.scraped = self._scraped
            ar.parsed = self._parsed
            ar.processed = self._processed
            ar.indexed = self._indexed
            ar.scr_module = self._scr_module
            ar.scr_class = self._scr_class
            ar.scr_version = self._scr_version
            ar.parser_version = self._parser_version
            ar.num_sentences = self._num_sentences
            ar.num_parsed = self._num_parsed
            ar.ambiguity = self._ambiguity
            ar.html = self._html
            ar.tree = self._tree
            ar.tokens = self._tokens
            if self._words is not None:
                # If the article has been parsed, update the index of word stems
                # (This may cause all stems for the article to be deleted, if
                # there are no successfully parsed sentences in the article)
                self._store_words(session)
            return True

    def prepare(self, enclosing_session=None, verbose=False, reload_parser=False):
        """ Prepare the article for display. If it's not already tokenized and parsed, do it now. """
        with SessionContext(enclosing_session, commit=True) as session:
            if self._tree is None or self._tokens is None:
                if reload_parser:
                    # We need a parse: Make sure we're using the newest grammar
                    self.reload_parser()
                self._parse(session, verbose=verbose)
                if self._tree is not None or self._tokens is not None:
                    # Store the updated article in the database
                    self.store(session)

    def parse(self, enclosing_session=None, verbose=False, reload_parser=False):
        """ Force a parse of the article """
        with SessionContext(enclosing_session, commit=True) as session:
            if reload_parser:
                # We need a parse: Make sure we're using the newest grammar
                self.reload_parser()
            self._parse(session, verbose=verbose)
            if self._tree is not None or self._tokens is not None:
                # Store the updated article in the database
                self.store(session)

    @property
    def url(self):
        return self._url

    @property
    def uuid(self):
        return self._uuid

    @property
    def heading(self):
        return self._heading

    @property
    def author(self):
        return self._author

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def parsed(self):
        return self._parsed

    @property
    def num_sentences(self):
        return self._num_sentences

    @property
    def num_parsed(self):
        return self._num_parsed

    @property
    def ambiguity(self):
        return self._ambiguity

    @property
    def root_domain(self):
        return self._root_domain

    @property
    def authority(self):
        return self._authority

    @property
    def html(self):
        return self._html

    @property
    def tree(self):
        return self._tree

    @property
    def tokens(self):
        return self._tokens

    @property
    def num_tokens(self):
        """ Count the tokens in the article and cache the result """
        if self._num_tokens is None:
            if self._raw_tokens is None and self._tokens:
                self._raw_tokens = json.loads(self._tokens)
            cnt = 0
            if self._raw_tokens:
                for p in self._raw_tokens:
                    for sent in p:
                        cnt += len(sent)
            self._num_tokens = cnt
        return self._num_tokens

    @staticmethod
    def token_stream(limit=None, skip_errors=True):
        """ Generator of a token stream consisting of `limit` sentences (or less) from the
            most recently parsed articles. After each sentence, None is yielded. """
        with SessionContext(commit=True, read_only=True) as session:

            q = (
                session
                .query(ArticleRow.url, ArticleRow.parsed, ArticleRow.tokens)
                .filter(ArticleRow.tokens != None)
                .order_by(desc(ArticleRow.parsed))
                .yield_per(200)
            )

            count = 0
            for a in q:
                doc = json.loads(a.tokens)
                for pg in doc:
                    for sent in pg:
                        if not sent:
                            continue
                        if skip_errors and any("err" in t for t in sent):
                            # Skip error sentences
                            continue
                        for t in sent:
                            # Yield the tokens
                            yield t
                        yield None  # End-of-sentence marker
                        # Are we done?
                        count += 1
                        if limit is not None and count >= limit:
                            return

    @staticmethod
    def sentence_stream(limit=None, skip=None, skip_errors=True):
        """ Generator of a sentence stream consisting of `limit` sentences (or less) from the
            most recently parsed articles. Each sentence is a list of token dicts. """
        with SessionContext(commit=True, read_only=True) as session:

            q = (
                session
                .query(ArticleRow.url, ArticleRow.parsed, ArticleRow.tokens)
                .filter(ArticleRow.tokens != None)
                .order_by(desc(ArticleRow.parsed))
                .yield_per(200)
            )

            count = 0
            skipped = 0
            for a in q:
                doc = json.loads(a.tokens)
                for pg in doc:
                    for sent in pg:
                        if not sent:
                            continue
                        if skip_errors and any("err" in t for t in sent):
                            # Skip error sentences
                            continue
                        if skip is not None and skipped < skip:
                            # If requested, skip sentences from the front (useful for test set)
                            skipped += 1
                            continue
                        # Yield the sentence as a fresh token list
                        yield [t for t in sent]
                        # Are we done?
                        count += 1
                        if limit is not None and count >= limit:
                            return

    @classmethod
    def articles(cls, criteria, enclosing_session=None):
        """ Generator of Article objects from the database that meet the given criteria """
        # The criteria are currently "timestamp", "author" and "domain",
        # as well as "order_by_parse" which if True indicates that the result
        # should be ordered with the most recently parsed articles first.
        with SessionContext(
            commit=True, read_only=True, session=enclosing_session
        ) as session:

            # Only fetch articles that have a parse tree
            q = session.query(ArticleRow).filter(ArticleRow.tree != None)

            # timestamp is assumed to contain a tuple: (from, to)
            if criteria and "timestamp" in criteria:
                ts = criteria["timestamp"]
                q = (
                    q
                    .filter(ArticleRow.timestamp >= ts[0])
                    .filter(ArticleRow.timestamp < ts[1])
                )

            if criteria and "author" in criteria:
                author = criteria["author"]
                q = q.filter(ArticleRow.author == author)

            if criteria and ("visible" in criteria or "domain" in criteria):
                # Need a join with Root for these criteria
                q = q.join(Root)
                if "visible" in criteria:
                    # Return only articles from roots with the specified visibility
                    visible = criteria["visible"]
                    assert isinstance(visible, bool)
                    q = q.filter(Root.visible == visible)
                if "domain" in criteria:
                    # Return only articles from the specified domain
                    domain = criteria["domain"]
                    assert isinstance(domain, str)
                    q = q.filter(Root.domain == domain)

            if criteria and criteria.get("order_by_parse"):
                # Order with newest parses first
                q = q.order_by(desc(ArticleRow.parsed))

            for arow in q.yield_per(500):
                yield cls._init_from_row(arow)

    @classmethod
    def all_matches(cls, criteria, pattern, enclosing_session=None):
        """ Generator of SimpleTree objects (see matcher.py) from articles matching
            the given criteria and the pattern """

        with SessionContext(
            commit=True, read_only=True, session=enclosing_session
        ) as session:

            # t0 = time.time()
            mcnt = acnt = tcnt = 0
            # print("Starting article loop")
            for a in cls.articles(criteria, enclosing_session=session):
                acnt += 1
                tree = Tree(url=a.url, authority=a.authority)
                tree.load(a.tree)
                for ix, simple_tree in tree.simple_trees():
                    tcnt += 1
                    for match in simple_tree.all_matches(pattern):
                        yield (a, ix, match)
                        mcnt += 1
            # t1 = time.time()
            # print("{0} articles with {1} trees examined, {2} matches in {3:.2f} seconds"
            #     .format(acnt, tcnt, mcnt, t1-t0))
