#!/usr/bin/env python
"""
    Reynir: Natural language processing for Icelandic

    Image retrieval module

    Copyright (c) 2016 Vilhjalmur Thorsteinsson
    All rights reserved
    See the accompanying README.md file for further licensing and copyright information.

    This module contains a function that retrieves the URL of an image corresponding to
    a (person) name. It uses a Google API on top of the Google Custom Search feature.

    Retrieved image information is cached in the database.

"""

import sys
import json
import logging
import urllib.request
import urllib.parse
from urllib.error import HTTPError
from datetime import datetime, timedelta
from collections import namedtuple
from contextlib import closing
from scraperdb import SessionContext, Link, BlacklistedLink

import requests
from io import BytesIO


def _server_query(url, q):
    """ Query a server via HTTP GET with a URL-encoded query string obtained from the dict q """
    doc = None
    if len(q):
        url += "?" + urllib.parse.urlencode(q)
    try:
        with closing(urllib.request.urlopen(url)) as response:
            if response:
                # Decode the HTML Content-type header to obtain the
                # document type and the charset (content encoding), if specified
                encoding = "ISO-8859-1"
                ctype = response.getheader("Content-type", "")
                if ";" in ctype:
                    s = ctype.split(";")
                    ctype = s[0]
                    enc = s[1].strip()
                    s = enc.split("=")
                    if s[0] == "charset" and len(s) == 2:
                        encoding = s[1]
                if ctype == "application/json":
                    doc = response.read()  # doc is a bytes object
                    if doc:
                        doc = doc.decode(encoding)
    except HTTPError as ex:
        logging.warning("server_query exception: {0}".format(ex))
    return doc


# The Google API identifier (you must obtain your own key if you want to use this code)
_API_KEY = ""
_API_KEY_PATH = "resources/GoogleServerKey.txt"


def _get_API_key():
    """ Read Google API key from file """
    global _API_KEY
    if not _API_KEY:
        try:
            # You need to obtain your own key and put it in
            # _API_KEY_PATH if you want to use this code
            with open(_API_KEY_PATH) as f:
                _API_KEY = f.read().rstrip()
        except FileNotFoundError as ex:
            _API_KEY = ""
    return _API_KEY


# Google Custom Search Engine identifier
_CX = "001858240983628375092:9aogptqla5e"

# The content type we're using in the links table
_CTYPE = "image-search-"

# Time (in days) before cached items expire
_CACHE_EXPIRATION_DAYS = 30

# Number of image URLs to fetch and store
_NUM_IMG_URLS = 10

# The returned image descriptor tuple
Img = namedtuple("Img", ["src", "width", "height", "link", "origin", "name"])


def get_image_url(
    name, hints=[], size="large", thumb=False, enclosing_session=None, cache_only=False
):
    """ Use Google Custom Search API to obtain an image corresponding to a (person) name """
    jdoc = None
    ctype = _CTYPE + size

    with SessionContext(commit=True, session=enclosing_session) as session:
        q = (
            session.query(Link.content, Link.timestamp)
            .filter(Link.ctype == ctype)
            .filter(Link.key == name)
            .one_or_none()
        )
        if q is not None:
            # Found in cache. If the result is old, purge it
            period = timedelta(days=_CACHE_EXPIRATION_DAYS)
            expired = datetime.utcnow() - q.timestamp > period
            if expired and not cache_only:
                _purge_single(name, ctype=ctype, enclosing_session=session)
            else:
                jdoc = q.content

        if not jdoc and cache_only:
            return None

        if not jdoc:
            # Not found in cache: prepare to ask Google
            key = _get_API_key()
            if not key:
                # No API key: can't ask for an image
                logging.warning("No API key for image lookup")
                return None

            # Assemble the query parameters
            search_str = '"{0}" {1}'.format(name, " ".join(hints)).strip()
            q = dict(
                q=search_str,
                num=_NUM_IMG_URLS,
                start=1,
                imgSize=size,
                # imgType = "face",   # Only images with faces
                lr="lang_is",  # Higher priority for Icelandic language pages
                gl="is",  # Higher priority for .is results
                searchType="image",
                cx=_CX,
                key=key,
            )
            jdoc = _server_query("https://www.googleapis.com/customsearch/v1", q)
            if jdoc:
                # Store in the cache
                l = Link(
                    ctype=ctype, key=name, content=jdoc, timestamp=datetime.utcnow()
                )
                session.add(l)

        if not jdoc:
            return None

        answer = json.loads(jdoc)

        if (
            answer
            and "items" in answer
            and answer["items"]
            and "link" in answer["items"][0]
        ):
            blacklist = _blacklisted_urls_for_key(name, enclosing_session=session)

            for item in answer["items"]:
                k = item["link"] if not thumb else item["image"]["thumbnailLink"]
                if k and item["link"] not in blacklist:
                    image = item["image"]
                    h = image["height"] if not thumb else image["thumbnailHeight"]
                    w = image["width"] if not thumb else image["thumbnailWidth"]
                    return Img(k, w, h, image["contextLink"], item["displayLink"], name)

    # No answer that makes sense
    return None


def blacklist_image_url(name, url):
    """ Blacklist image URL for a given key """

    with SessionContext(commit=True) as session:
        # Verify that URL exists in DB
        if not _get_cached_entry(name, url, enclosing_session=session):
            return

        # Check if already blacklisted
        if url in _blacklisted_urls_for_key(name, enclosing_session=session):
            return

        # Add to blacklist
        b = BlacklistedLink(
            key=name, url=url, link_type="image", timestamp=datetime.utcnow()
        )
        session.add(b)

        return get_image_url(name, enclosing_session=session)


def update_broken_image_url(name, url):
    """ Refetch image URL for name if broken """

    with SessionContext() as session:
        # Verify that URL exists in DB
        r = _get_cached_entry(name, url, enclosing_session=session)

        if r:
            # Verify that URL is indeed broken
            if not check_image_url(url):
                # Blacklist the URL, purge results from cache and refetch
                blacklist_image_url(name, url)
                _purge_single(name, ctype=r.ctype, enclosing_session=session)
                return get_image_url(name)

    return None


def check_image_url(url):
    """ Check if image exists at URL by sending HEAD request """
    req = urllib.request.Request(url, method="HEAD")
    try:
        response = urllib.request.urlopen(req, timeout=2.0)
        return response.status == 200
    except:
        pass

    return False


def _blacklisted_urls_for_key(key, enclosing_session=None):
    """ Fetch blacklisted urls for a given key """
    with SessionContext(commit=True, session=enclosing_session) as session:
        q = (
            session.query(BlacklistedLink.url)
            .filter(BlacklistedLink.link_type == "image")
            .filter(BlacklistedLink.key == key)
            .all()
        )
        return [r for (r,) in q]


def _get_cached_entry(name, url, enclosing_session=None):
    """ Fetch cached entry by key and url """
    with SessionContext(commit=True, session=enclosing_session) as session:
        # TODO: content column should be converted to jsonb
        # from varchar to query faster & more intelligently
        return (
            session.query(Link)
            .filter(Link.key == name)
            .filter(Link.content.like("%" + url + "%"))
            .one_or_none()
        )


def _purge_single(key, ctype=None, enclosing_session=None):
    """ Remove cache entry """
    with SessionContext(commit=True, session=enclosing_session) as session:
        filters = [Link.key == key]
        if ctype:
            filters.append(Link.ctype == ctype)

        session.query(Link).filter(*filters).delete()


def _purge():
    """ Remove all cache entries """
    if input("Purge all cached data? (y/n): ").lower().startswith("y"):
        with SessionContext(commit=True) as session:
            session.query(Link).delete()


STATICMAP_URL = (
    "https://maps.googleapis.com/maps/api/staticmap?"
    "zoom={0}&style=feature:poi%7Cvisibility:off"
    "&size={1}x{2}&language=is&scale=2&maptype=roadmap"
    "&key={3}&markers={4},{5}"
)


def get_staticmap_image(latitude, longitude, zoom=6, width=180, height=180):
    """ Request image from Google Static Maps API, return image data as bytes """
    key = _get_API_key()
    if not key:
        return None

    url = STATICMAP_URL.format(zoom, width, height, key, latitude, longitude)
    # TODO: Use urllib instead of requests here
    try:
        r = requests.get(url, stream=True)
    except Exception as e:
        logging.warning(str(e))
        return None

    if r.status_code == 200:
        r.raw.decode_content = True
        return BytesIO(r.raw.data)

    logging.warning("Status {0} when requesting static map image".format(r.status_code))
    return None


def _test():
    """ Test image lookup """
    print("Testing...")
    print("Bjarni Benediktsson")
    img = get_image_url("Bjarni Benediktsson")
    print("{0}".format(img))

    print("Vilhjálmur Þorsteinsson")
    img = get_image_url("Vilhjálmur Þorsteinsson")
    print("{0}".format(img))

    print("Blængur Klængsson Eyfjörð")
    img = get_image_url("Blængur Klængsson Eyfjörð")
    print("{0}".format(img))  # Should be None


if __name__ == "__main__":

    cmap = {"test": _test, "purge": _purge}

    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    if cmd in cmap.keys():
        cmap[cmd]()
    elif cmd:
        # Any other arg is a name to fetch an image for
        img = get_image_url(cmd)
        print("{0}".format(img))
