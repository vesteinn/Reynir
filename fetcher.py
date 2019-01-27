"""
    Reynir: Natural language processing for Icelandic

    Fetcher module

    Copyright (c) 2018 Miðeind ehf.

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


    This module contains utility classes for web page fetching and tokenization.

"""

import re
import importlib

import requests
import urllib.parse as urlparse
from urllib.error import HTTPError

from bs4 import BeautifulSoup, NavigableString

from nertokenizer import tokenize_and_recognize
from scraperdb import SessionContext, Root, Article as ArticleRow


# The HTML parser to use with BeautifulSoup
# _HTML_PARSER = "html5lib"
_HTML_PARSER = "html.parser"


class Fetcher:

    """ The worker class that scrapes the known roots """

    # HTML tags that we explicitly don't want to look at
    _EXCLUDE_TAGS = frozenset(["script", "audio", "video", "style"])

    # HTML tags that typically denote blocks (DIV-like), not inline constructs (SPAN-like)
    _BLOCK_TAGS = frozenset(
        [
            "p",
            "h1",
            "h2",
            "h3",
            "h4",
            "div",
            "main",
            "article",
            "header",
            "section",
            "table",
            "thead",
            "tbody",
            "tr",
            "td",
            "ul",
            "li",
            "form",
            "option",
            "input",
            "label",
            "figure",
            "figcaption",
            "footer",
        ]
    )

    _INLINE_BLOCK_TAGS = frozenset(["span"])  # Inserted with whitespace

    _WHITESPACE_TAGS = frozenset(["img"])  # Inserted as whitespace

    _BREAK_TAGS = frozenset(["br", "hr"])  # Cause paragraph breaks at outermost level

    # Cache of instantiated scrape helpers
    _helpers = dict()

    def __init__(self):
        """ No instances are supposed to be created of this class """
        assert False

    class TextList:

        """ Accumulates raw text blocks and eliminates unnecessary nesting indicators """

        def __init__(self):
            self._result = []
            self._nesting = 0
            self._white = False

        def append(self, w):
            if self._nesting > 0:
                if w.isspace():
                    # Whitespace is not reason to emit nesting markers
                    return
                self._result.append(" [[ " * self._nesting)
                self._nesting = 0
            self._result.append(w)
            self._white = False

        def append_whitespace(self):
            if self._nesting == 0:
                # No need to append whitespace if we're just inside a begin-block
                if not self._white:
                    self._result.append(" ")
                    self._white = True

        def begin(self):
            self._nesting += 1

        def end(self):
            if self._nesting > 0:
                self._nesting -= 1
            else:
                self._result.append(" ]] ")
                self._white = True

        def insert_break(self):
            """ Used to cut paragraphs at <br> and <hr> tags """
            if self._nesting == 0:
                self._result.append(" ]] [[ ")
                self._white = True

        def result(self):
            """ Return the accumulated result as a string """
            assert self._nesting == 0
            text = "".join(self._result)
            # Eliminate soft hyphen and zero width space characters
            text = re.sub("\u00AD|\u200B", "", text)
            # Eliminate consecutive whitespace
            return re.sub(r"\s+", " ", text)

    @staticmethod
    def mark_paragraphs(txt):
        """ Insert paragraph markers into plaintext, by newlines """
        return "[[ " + " ]] [[ ".join(txt.split("\n")) + " ]]"

    @staticmethod
    def extract_text(soup, result):
        """ Append the human-readable text found in an HTML soup to the result TextList """
        if soup is None:
            return
        for t in soup.children:
            if type(t) == NavigableString:
                # Text content node
                result.append(t)
            elif isinstance(t, NavigableString):
                # Comment, CDATA or other text data: ignore
                pass
            elif t.name in Fetcher._BREAK_TAGS:
                result.insert_break()
                # html.parser (erroneously) nests content inside
                # <br> and <hr> tags
                Fetcher.extract_text(t, result)
            elif t.name in Fetcher._WHITESPACE_TAGS:
                # Tags that we interpret as whitespace, such as <img>
                result.append_whitespace()
                # html.parser nests content inside <img> tags if
                # they are not explicitly closed
                Fetcher.extract_text(t, result)
            elif t.name in Fetcher._BLOCK_TAGS:
                # Nested block tag
                result.begin()  # Begin block
                Fetcher.extract_text(t, result)
                result.end()  # End block
            elif t.name in Fetcher._INLINE_BLOCK_TAGS:
                # Put whitespace around the inline block
                # so that words don't run together
                result.append_whitespace()
                Fetcher.extract_text(t, result)
                result.append_whitespace()
            elif t.name not in Fetcher._EXCLUDE_TAGS:
                # Non-block tag
                Fetcher.extract_text(t, result)

    @staticmethod
    def to_tokens(soup, enclosing_session=None):
        """ Convert an HTML soup root into a parsable token stream """

        # Extract the text content of the HTML into a list
        tlist = Fetcher.TextList()
        Fetcher.extract_text(soup, tlist)
        text = tlist.result()

        # Tokenize the resulting text, returning a generator
        return tokenize_and_recognize(text, enclosing_session=enclosing_session)

    @classmethod
    def raw_fetch_url(cls, url):
        """ Low-level fetch of an URL, returning a decoded string """
        html_doc = None
        try:

            # Normal external HTTP/HTTPS fetch
            r = requests.get(url)
            if r is None:
                print("No document returned for URL {0}".format(url))
                return None
            if r.status_code == requests.codes.ok:
                html_doc = r.text
            else:
                print("HTTP status {0} for URL {1}".format(r.status_code, url))

        except requests.exceptions.ConnectionError as e:
            print("ConnectionError: {0} for URL {1}".format(e, url))
            html_doc = None
        except requests.exceptions.ChunkedEncodingError as e:
            print("ChunkedEncodingError: {0} for URL {1}".format(e, url))
            html_doc = None
        except HTTPError as e:
            print("HTTPError: {0} for URL {1}".format(e, url))
            html_doc = None
        except UnicodeEncodeError as e:
            print(
                "Exception when opening URL {0}: {1}"
                .format(url, e)
            )
            html_doc = None
        except UnicodeDecodeError as e:
            print(
                "Exception when decoding HTML of {0}: {1}"
                .format(url, e)
            )
            html_doc = None
        return html_doc

    @classmethod
    def _get_helper(cls, root):
        """ Return a scrape helper instance for the given root """
        # Obtain an instance of a scraper helper class for this root
        helper_id = root.scr_module + "." + root.scr_class
        if helper_id in Fetcher._helpers:
            # Already instantiated a helper: get it
            helper = Fetcher._helpers[helper_id]
        else:
            # Dynamically instantiate a new helper class instance
            mod = importlib.import_module(root.scr_module)
            helper_class = getattr(mod, root.scr_class, None) if mod else None
            helper = helper_class(root) if helper_class else None
            Fetcher._helpers[helper_id] = helper
            if not helper:
                print("Unable to instantiate helper {0}".format(helper_id))
        return helper

    @staticmethod
    def make_soup(doc, helper=None):
        """ Convert a document to a soup, using the helper if available """
        if helper is None:
            soup = BeautifulSoup(doc, _HTML_PARSER) if doc else None
            if soup is None or soup.html is None:
                return None
        else:
            soup = helper.make_soup(doc) if doc else None
        return soup

    @classmethod
    def tokenize_html(cls, url, html, enclosing_session=None):
        """ Convert HTML into a token iterable (generator) """
        with SessionContext(enclosing_session) as session:
            helper = cls.helper_for(session, url)
            soup = Fetcher.make_soup(html, helper)
            if soup is None:
                content = None
            elif helper is None:
                content = soup.html.body
            else:
                content = helper.get_content(soup)
            # Convert the content soup to a token iterable (generator)
            return (
                Fetcher.to_tokens(content, enclosing_session=session)
                if content
                else None
            )

    @staticmethod
    def children(root, soup):
        """ Return a set of child URLs within a HTML soup, relative to the given root """
        # Establish the root URL base parameters
        root_s = urlparse.urlsplit(root.url)
        root_url = urlparse.urlunsplit(root_s)
        root_url_slash = urlparse.urlunsplit(
            (root_s.scheme, root_s.netloc, "/", root_s.query, "")
        )
        # Collect all interesting <a> tags from the soup and obtain their href-s:
        fetch = set()
        for link in soup.find_all("a"):
            href = link.get("href")
            if not href:
                continue
            # Split the href into its components
            s = urlparse.urlsplit(href)
            if s.scheme and s.scheme not in {"http", "https"}:
                # Not HTTP
                continue
            if s.netloc and not (
                s.netloc == root.domain or s.netloc.endswith("." + root.domain)
            ):
                # External domain - we're not interested
                continue
            # Seems to be a bug in urllib: fragments are put into the
            # path if there is no canonical path
            newpath = s.path
            if newpath.startswith("#") or newpath.startswith("/#"):
                newpath = ""
            if not newpath and not s.query:
                # No meaningful path info present
                continue
            # Make sure the newpath is properly urlencoded
            if newpath:
                newpath = urlparse.quote(newpath)
            # Fill in missing stuff from the root URL base parameters
            newurl = (
                s.scheme or root_s.scheme,
                s.netloc or root_s.netloc,
                newpath,
                s.query,
                ""
            )
            # Make a complete new URL to fetch
            url = urlparse.urlunsplit(newurl)
            if url in {root_url, root_url_slash}:
                # Exclude the root URL
                continue
            # Looks legit: add to the fetch set
            fetch.add(url)
        return fetch

    @classmethod
    def helper_for(cls, session, url):
        """ Return a scrape helper for the root of the given url """
        s = urlparse.urlsplit(url)
        root = None
        # Find which root this URL belongs to, if any
        for r in session.query(Root).all():
            root_s = urlparse.urlsplit(r.url)
            # Find the root of the domain, i.e. www.ruv.is -> ruv.is
            root_domain = ".".join(root_s.netloc.split(".")[-2:])
            # This URL belongs to a root if the domain (netloc) part
            # ends with the root domain
            if s.netloc == root_domain or s.netloc.endswith("." + root_domain):
                root = r
                break
        # Obtain a scrape helper for the root, if any
        return cls._get_helper(root) if root else None

    # noinspection PyComparisonWithNone
    @classmethod
    def find_article(cls, url, enclosing_session=None):
        """ Return a scraped article object, if found, else None """
        with SessionContext(enclosing_session, commit=True) as session:
            article = (
                session
                .query(ArticleRow)
                .filter_by(url=url)
                .filter(ArticleRow.scraped != None)
                .one_or_none()
            )
        return article

    # noinspection PyComparisonWithNone
    @classmethod
    def is_known_url(cls, url, session=None):
        """ Return True if the URL has already been scraped """
        return cls.find_article(url, session) is not None

    @classmethod
    def fetch_article(cls, url, enclosing_session=None):
        """ Fetch a previously scraped article, returning
            a tuple (article, metadata, content) or None if error """

        with SessionContext(enclosing_session) as session:

            article = cls.find_article(url, session)
            if article is None:
                return (None, None, None)

            html_doc = article.html
            if not html_doc:
                return (None, None, None)

            helper = cls.helper_for(session, url)
            # Parse the HTML
            soup = Fetcher.make_soup(html_doc, helper)
            if soup is None:
                print("Fetcher.fetch_article({0}): No soup".format(url))
                return (None, None, None)

            # Obtain the metadata and the content from the resulting soup
            metadata = helper.get_metadata(soup) if helper else None
            content = helper.get_content(soup) if helper else soup.html.body
            return (article, metadata, content)

    @classmethod
    def fetch_url(cls, url, enclosing_session=None):
        """ Fetch a URL using the scraping mechanism, returning
            a tuple (metadata, content) or None if error """

        with SessionContext(enclosing_session) as session:

            helper = cls.helper_for(session, url)

            if helper is None or not hasattr(helper, "fetch_url"):
                # Do a straight HTTP fetch
                html_doc = cls.raw_fetch_url(url)
            else:
                # Hand off to the helper
                html_doc = helper.fetch_url(url)

            if not html_doc:
                return None

            # Parse the HTML
            soup = Fetcher.make_soup(html_doc, helper)
            if soup is None:
                print("Fetcher.fetch_url({0}): No soup or no soup.html".format(url))
                return None

            # Obtain the metadata and the content from the resulting soup
            metadata = helper.get_metadata(soup) if helper else None
            content = helper.get_content(soup) if helper else soup.html.body
            return (metadata, content)

    @classmethod
    def fetch_url_html(cls, url, enclosing_session=None):
        """ Fetch a URL using the scraping mechanism, returning
            a tuple (html, metadata, helper) or None if error """

        with SessionContext(enclosing_session) as session:

            helper = cls.helper_for(session, url)

            if helper is None or not hasattr(helper, "fetch_url"):
                # Do a straight HTTP fetch
                html_doc = cls.raw_fetch_url(url)
            else:
                # Hand off to the helper
                html_doc = helper.fetch_url(url)

            if not html_doc:
                return (None, None, None)

            # Parse the HTML
            soup = Fetcher.make_soup(html_doc, helper)
            if soup is None:
                print("Fetcher.fetch_url_html({0}): No soup".format(url))
                return (None, None, None)

            # Obtain the metadata from the resulting soup
            metadata = helper.get_metadata(soup) if helper else None
            return (html_doc, metadata, helper)
