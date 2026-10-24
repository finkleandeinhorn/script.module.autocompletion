# -*- coding: utf8 -*-

# Copyright (C) 2015 - Philipp Temminghoff <phil65@kodi.tv>
# This program is Free Software see LICENSE file for details

from abc import ABC, abstractmethod
from urllib.parse import quote_plus
import os
import re
import sqlite3
import time
import hashlib
import requests
import json

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

SCRIPT_ID = "script.module.autocompletion"
SCRIPT_ADDON = xbmcaddon.Addon(SCRIPT_ID)
PLUGIN_ID = "plugin.program.autocompletion"
PLUGIN_ADDON = xbmcaddon.Addon(PLUGIN_ID)
SETTING = PLUGIN_ADDON.getSetting
ADDON_PATH = xbmcvfs.translatePath(SCRIPT_ADDON.getAddonInfo("path"))
ADDON_ID = SCRIPT_ADDON.getAddonInfo("id")
ADDON_DATA_PATH = xbmcvfs.translatePath(SCRIPT_ADDON.getAddonInfo("profile"))


def get_autocomplete_items(search_str, limit=10, provider=None, source=None):
    """
    get dict list with autocomplete
    """
    if xbmc.getCondVisibility("System.HasHiddenInput"):
        return []

    if source is None:
        if xbmcgui.Window(10000).getProperty("AutoCompleteSource") == "library":
            source = "library"
        elif xbmc.getCondVisibility("Window.IsActive(addonbrowser)"):
            source = "addons"
    setting = source if source in ("library", "addons") else SETTING("autocomplete_provider").lower()

    if setting == "library":
        provider = LocalLibraryProvider(limit=limit)
    elif setting == "addons":
        provider = InstalledAddonsProvider(limit=limit)
    elif setting == "youtube":
        provider = GoogleProvider(youtube=True, limit=limit)
    elif setting == "google":
        provider = GoogleProvider(limit=limit)
    elif setting == "bing":
        provider = BingProvider(limit=limit)
    elif setting == "tmdb":
        provider = TmdbProvider(limit=limit)
    else:
        provider = LocalDictProvider(limit=limit)
    provider.limit = limit
    return provider.get_predictions(search_str)


def prep_search_str(text):
    for char in text:
        if 1488 <= ord(char) <= 1514:
            return text[::-1]
    return text


class BaseProvider(ABC):

    HEADERS = {'User-agent': 'Mozilla/5.0'}

    def __init__(self, *args, **kwargs):
        self.limit = kwargs.get("limit", 10)
        self.language = SETTING("autocomplete_lang")

    @abstractmethod
    def build_url(self, query):
        pass

    def get_predictions(self, search_str):
        if not search_str:
            return []
        items = []
        result = self.fetch_data(search_str)
        for i, item in enumerate(result):
            li = {"label": item, "search_string": prep_search_str(item)}
            items.append(li)
            if i > int(self.limit):
                break
        return items

    def get_prediction_listitems(self, search_str):
        for item in self.get_predictions(search_str):
            li = {"label": item, "search_string": search_str}
            yield li

    def fetch_data(self, search_str):
        url = self.build_url(quote_plus(search_str))
        result = get_JSON_response(url=self.BASE_URL.format(endpoint=url), headers=self.HEADERS, folder=self.FOLDER)
        return self.process_result(result)

    def process_result(self, result):
        if not result or len(result) <= 1:
            return []
        else:
            return result[1] if isinstance(result[1], list) else result


class GoogleProvider(BaseProvider):

    BASE_URL = "http://clients1.google.com/complete/{endpoint}"
    FOLDER = "Google"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.youtube = kwargs.get("youtube", False)

    def build_url(self, query):
        url = f"search?hl={self.language}&q={query}&json=t&client=serp"
        if self.youtube:
            url += "&ds=yt"
        return url


class BingProvider(BaseProvider):

    BASE_URL = "http://api.bing.com/osjson.aspx?{endpoint}"
    FOLDER = "Bing"

    def __init__(self, *args, **kwargs):
        super(BingProvider, self).__init__(*args, **kwargs)

    def build_url(self, query):
        url = f"query={query}"
        return url


class TmdbProvider(BaseProvider):

    BASE_URL = "https://www.themoviedb.org/search/multi?{endpoint}"
    FOLDER = "TMDB"

    def __init__(self, *args, **kwargs):
        super(TmdbProvider, self).__init__(*args, **kwargs)

    def build_url(self, query):
        url = f"language={self.language}&query={query}"
        return url

    def process_result(self, result):
        if not result or not result.get("results"):
            return []
        out = []
        results = result.get("results")
        for i in results:
            title = None
            media_type = i.get("media_type")
            if media_type == "movie":
                title = i["title"]
            elif media_type in ["tv", "person"]:
                title = i["name"]
            else:
                title = i
            out.append(title)
        return out


class LocalDictProvider(BaseProvider):
    def __init__(self, *args, **kwargs):
        super(LocalDictProvider, self).__init__(*args, **kwargs)
        local = SETTING("autocomplete_lang_local")
        if local:
            self.language = local
        else:
            self.language = "en"

    def build_url(self, query):
        return super().build_url(query)

    def fetch_data(self, search_str):
        k = search_str.rfind(" ")
        if k >= 0:
            search_str = search_str[k + 1 :]

        path = os.path.join(ADDON_PATH, "resources", "data", f"common_{self.language}.txt")
        suggestions = []

        with xbmcvfs.File(path) as f:
            for line in f.read().split('\n'):
                if not line.startswith(search_str) or len(line) <= 2:
                    continue
                suggestions.append(line)
                if len(suggestions) > int(self.limit):
                    break

        return suggestions


class InstalledAddonsProvider(BaseProvider):
    """
    Suggestions drawn from the names of the installed add-ons, used while searching
    in the add-on browser. Matches at the start of the name or of any word within it.
    """

    # internal add-on types that never show up in the add-on browser
    HIDDEN_TYPES = ("xbmc.python.module", "xbmc.python.library", "kodi.resource", "xbmc.addon.metadata")

    def build_url(self, query):
        return query

    def fetch_data(self, search_str):
        needle = search_str.strip().lower()
        if not needle:
            return []
        query = {
            "jsonrpc": "2.0", "id": 1, "method": "Addons.GetAddons",
            "params": {"installed": True, "enabled": "all", "properties": ["name"]},
        }
        response = json.loads(xbmc.executeJSONRPC(json.dumps(query)))
        found = {}
        for addon in response.get("result", {}).get("addons") or []:
            if addon.get("type", "").startswith(self.HIDDEN_TYPES):
                continue
            name = (addon.get("name") or "").strip()
            key = name.lower()
            if not name or key in found:
                continue
            words = key.split()
            if key == needle:
                tier = 0
            elif key.startswith(needle):
                tier = 1
            elif any(" ".join(words[i:]).startswith(needle) for i in range(1, len(words))):
                tier = 2
            else:
                continue
            found[key] = (tier, key, name)
        return [entry[2] for entry in sorted(found.values())]


class LocalLibraryProvider(BaseProvider):
    """
    Suggestions drawn from the local video and music library only:
    titles, performers (actors, directors, artists), albums and songs.
    Matches at the start of the title or of any word within it.
    """

    # (script.globalsearch category setting, database prefix, table, column, extra condition);
    # a source is only used while its category is enabled in Global Search, so
    # suggestions never offer something that search would not find.
    # Order is the tie-break between sources.
    SOURCES = (
        ("movies", "MyVideos", "movie", "c00", ""),
        ("tvshows", "MyVideos", "tvshow", "c00", ""),
        ("actors", "MyVideos", "actor", "name",
         "actor_id IN (SELECT actor_id FROM actor_link WHERE media_type = 'movie')"),
        ("tvactors", "MyVideos", "actor", "name",
         "actor_id IN (SELECT actor_id FROM actor_link WHERE media_type = 'tvshow')"),
        ("directors", "MyVideos", "actor", "name",
         "actor_id IN (SELECT actor_id FROM director_link WHERE media_type = 'movie')"),
        ("musicvideos", "MyVideos", "musicvideo", "c00", ""),
        ("artists", "MyMusic", "artist", "strArtist", ""),
        ("albums", "MyMusic", "album", "strAlbum", ""),
        ("songs", "MyMusic", "song", "strTitle", ""),
        ("episodes", "MyVideos", "episode", "c00", ""),
    )
    ROW_LIMIT = 30

    def build_url(self, query):
        return query

    @staticmethod
    def _db_path(prefix):
        folder = xbmcvfs.translatePath("special://database")
        best, best_version = None, -1
        for name in os.listdir(folder):
            match = re.fullmatch(prefix + r"(\d+)\.db", name)
            if match and int(match.group(1)) > best_version:
                best, best_version = name, int(match.group(1))
        return os.path.join(folder, best) if best else None

    @staticmethod
    def _category_enabled(category):
        try:
            return xbmcaddon.Addon("script.globalsearch").getSettingBool(category)
        except Exception:
            return True

    def fetch_data(self, search_str):
        text = search_str.strip()
        if not text:
            return []
        escaped = re.sub(r"([\\%_])", r"\\\1", text)
        prefix_like, word_like = escaped + "%", "% " + escaped + "%"
        needle = text.lower()
        connections = {}
        found = {}
        try:
            for rank, (category, db_prefix, table, column, condition) in enumerate(self.SOURCES):
                if not self._category_enabled(category):
                    continue
                if db_prefix not in connections:
                    path = self._db_path(db_prefix)
                    connections[db_prefix] = (
                        sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1) if path else None
                    )
                con = connections[db_prefix]
                if not con:
                    continue
                query = (
                    "SELECT %(c)s FROM %(t)s WHERE (%(c)s LIKE ?1 ESCAPE '\\' OR %(c)s LIKE ?2 ESCAPE '\\') %(x)s "
                    "ORDER BY %(c)s NOT LIKE ?1 ESCAPE '\\', %(c)s LIMIT %(n)d"
                ) % {"c": column, "t": table, "x": "AND " + condition if condition else "", "n": self.ROW_LIMIT}
                try:
                    rows = con.execute(query, (prefix_like, word_like)).fetchall()
                except sqlite3.Error as e:
                    log("query on %s failed: %s" % (table, e))
                    continue
                for (label,) in rows:
                    label = (label or "").strip()
                    key = label.lower()
                    if label and key not in found:
                        # exact match, then whole-title prefix, then word-start matches
                        tier = 0 if key == needle else 1 if key.startswith(needle) else 2
                        found[key] = (tier, rank, key, label)
        except sqlite3.Error as e:
            log("could not open library database: %s" % e)
        finally:
            for con in connections.values():
                if con:
                    con.close()
        return [entry[3] for entry in sorted(found.values())]


def get_JSON_response(url="", cache_days=7.0, folder=False, headers=False):
    """
    get JSON response for *url, makes use of file cache.
    """
    now = time.time()
    hashed_url = hashlib.md5(url.encode("utf-8")).hexdigest()
    cache_path = xbmcvfs.translatePath(os.path.join(ADDON_DATA_PATH, folder) if folder else ADDON_DATA_PATH)
    path = os.path.join(cache_path, f"{hashed_url}.txt")
    cache_seconds = int(cache_days * 86400)
    results = []

    if xbmcvfs.exists(path) and ((now - os.path.getmtime(path)) < cache_seconds):
        results = read_from_file(path)
        log(f"loaded file for {url}. time: {float(time.time() - now)}")
    else:
        response = get_http(url, headers)
        try:
            results = json.loads(response)
            log(f"download {url}. time: {float(time.time() - now)}")
            save_to_file(results, hashed_url, cache_path)
        except Exception:
            log(f"Exception: Could not get new JSON data from {url}. Trying to fallback to cache")
            log(response)
            results = read_from_file(path)

    return results


def get_http(url, headers):
    """
    fetches data from *url, returns it as a string
    """
    succeed = 0
    monitor = xbmc.Monitor()
    while (succeed < 2) and (not monitor.abortRequested()):
        try:
            response = requests.get(url, headers=headers)
            if not response.ok:
                raise Exception
            return response.text
        except Exception:
            log(f"get_http: could not get data from {url}")
            monitor.waitForAbort(1)
            succeed += 1
    return None


def read_from_file(path="", raw=False):
    """
    return data from file with *path
    """
    if not xbmcvfs.exists(path):
        return []

    try:
        with xbmcvfs.File(path) as f:
            log(f"opened textfile {path}.")
            if raw:
                return f.read()
            else:
                return json.load(f)
    except Exception:
        log(f"failed to load textfile: {path}")
        return []


def log(txt):
    message = f"{ADDON_ID}: {txt}"
    xbmc.log(msg=message, level=xbmc.LOGDEBUG)


def save_to_file(content, filename, path=""):
    """
    dump json and save to *filename in *path
    """
    if not xbmcvfs.exists(path):
        xbmcvfs.mkdirs(path)

    text_file_path = os.path.join(path, f"{filename}.txt")
    now = time.time()

    with xbmcvfs.File(text_file_path, "w") as text_file:
        json.dump(content, text_file)

    log(f"saved textfile {text_file_path}. Time: {float(time.time() - now)}")
    return True
