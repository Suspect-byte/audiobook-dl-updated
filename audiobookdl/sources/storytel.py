from curl_cffi.requests.models import Response
from .source import Source
from audiobookdl import (
    AudiobookFile,
    Chapter,
    logging,
    AudiobookMetadata,
    Cover,
    Audiobook,
    Series,
    BookId,
    Result,
)
from audiobookdl.exceptions import (
    GenericAudiobookDLException,
    UserNotAuthorized,
    CloudflareBlocked,
    BookNotFound,
    BookHasNoAudiobook,
    BookNotReleased,
    DataNotPresent,
)
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from typing import Any, List, Dict, Optional, Union
from urllib3.util import parse_url
from urllib.parse import urlunparse, parse_qs
from datetime import datetime, date
import pycountry
import json
import re
import os
import uuid
import traceback

# Keep the metadata_corrections as in the original file
# fmt: off
metadata_corrections: Dict[str, Dict[str, Any]] = {
    "books": {
        "1623721": { "title": "Bibi & Tina: Schatten über dem Martinshof", "release_date": date(2010,3,12) },
    }
}
# fmt: on

# path data of the headphone icon on the website used to identify audiobooks
svg_headphone_path = "M8.25 12.371h-.625c-1.38 0-2.5 1.121-2.5 2.505v3.12a2.503 2.503 0 0 0 2.5 2.504h.625c.69 0 1.25-.56 1.25-1.252v-5.627c0-.691-.559-1.25-1.25-1.25Zm-.625 6.254a.628.628 0 0 1-.625-.63v-3.12c0-.347.28-.63.625-.63v4.38ZM12 3C6.41 3 2.178 7.652 2 13v4.375c0 .346.28.625.625.625h.625a.626.626 0 0 0 .625-.627V13c0-4.48 3.646-8.117 8.125-8.117 4.48 0 8.125 3.637 8.125 8.117v4.371c-.035.348.281.629.625.629l.625.001c.346 0 .625-.28.625-.625v-4.411C21.82 7.652 17.59 3 12 3Zm4.375 9.371h-.625c-.69 0-1.25.56-1.25 1.252v5.625c0 .692.56 1.252 1.25 1.252h.625c1.38 0 2.5-1.121 2.5-2.505v-3.12a2.503 2.503 0 0 0-2.5-2.504ZM17 17.996a.628.628 0 0 1-.625.629v-4.379c.345 0 .625.283.625.63v3.12Z"


class StorytelSource(Source):
    match = [
        r"https?://(?:www.)?(?:storytel|mofibo).com/(?P<language>\w+)(?:/(?P<language2>\w+))?/(?P<list_type>(?:books|series|authors|narrators|publishers|categories))/.+",
    ]
    names = ["Storytel", "Mofibo"]
    _authentication_methods = [
        "login",
    ]
    _download_counter = 0
    create_storage_dir = True

    def __init__(self, options) -> None:
        super().__init__(options)
        self.ebook = getattr(options, 'ebook', None)
        
        # Add debug flag
        self.debug = getattr(options, 'debug', False)

        self.database_directory_books = os.path.join(self.database_directory, "books")
        self.database_directory_playback_metadata = os.path.join(
            self.database_directory, "playback-metadata"
        )
        self.database_directory_lists = os.path.join(self.database_directory, "lists")
        os.makedirs(self.database_directory_books, exist_ok=True)
        os.makedirs(self.database_directory_playback_metadata, exist_ok=True)
        os.makedirs(self.database_directory_lists, exist_ok=True)

    def _get_book_path(self, consumableId: str) -> str:
        return os.path.join(self.database_directory_books, f"{consumableId}.json")

    def _get_playback_metadata_path(self, consumableId: str) -> str:
        return os.path.join(
            self.database_directory_playback_metadata, f"{consumableId}.json"
        )

    def _get_lists_path(self, list_name: str, languages: str, formats: str) -> str:
        return os.path.join(
            self.database_directory_lists, f"{list_name}_{languages}_{formats}.json"
        )

    def _skip_download_check(self, book_id: str) -> bool:
        if self.skip_downloaded:
            book_path = self._get_book_path(book_id)
            return os.path.exists(book_path)
        else:
            return False

    @staticmethod
    def encrypt_password(password: str) -> str:
        key = b"VQZBJ6TD8M9WBUWT"
        iv = b"joiwef08u23j341a"
        msg = pad(password.encode(), AES.block_size)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        cipher_text = cipher.encrypt(msg)
        return cipher_text.hex()

    def check_cloudflare_blocked(self, response: Response) -> None:
        if response.status_code == 403:
            error_str = "<title>Attention Required! | Cloudflare</title>"
            if error_str in response.text:
                raise CloudflareBlocked

    def _login(self, url: str, username: str, password: str) -> None:
        self._url = url
        self._username = username
        self._password = self.encrypt_password(password)
        self._session.headers.update(
            {
                "User-Agent": "Storytel/24.52 (Android 14; Google Pixel 8 Pro) Release/2288809",
            }
        )
        self._do_login()

    def _do_login(self) -> None:
        generated_device_id = str(uuid.uuid4())

        resp = self._session.post(
            f"https://www.storytel.com/api/login.action?m=1&token=guestsv&userid=-1&version=24.52"
            f"&terminal=android&locale=sv&deviceId={generated_device_id}&kidsMode=false",
            data={
                "uid": self._username,
                "pwd": self._password,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code != 200:
            if resp.status_code == 403:
                self.check_cloudflare_blocked(resp)
            raise UserNotAuthorized

        user_data = resp.json()
        jwt = user_data["accountInfo"]["jwt"]
        self._language = user_data["accountInfo"]["lang"]
        self._session.headers.update({"authorization": f"Bearer {jwt}"})

    def _relogin_check(self) -> None:
        if self._download_counter > 0 and self._download_counter % 10 == 0:
            logging.debug("refreshing login")
            self._do_login()

    @staticmethod
    def _clean_share_url(url: str) -> str:
        return url.split("?")[0]

    def download_from_id(self, book_id: str) -> Audiobook:
        self._relogin_check()
        audiobook = self.download_book_from_book_id(book_id)
        return audiobook

    def download(self, url: str) -> Result:
        self._relogin_check()

        if m := re.match(self.match[0], url):
            language, language2, list_type = m.groups()
            logging.debug(f"download: {url=}, {list_type=}, {language=}, {language2=}")
            # individual books
            if list_type == "books":
                try:
                    return self.download_book_from_url(url)
                except BookHasNoAudiobook:
                    # Check if this is an ebook-only book
                    consumableId = self.get_id_from_url(url)
                    book_details = self.download_book_details(consumableId)
                    has_ebook = any(f["type"] == "ebook" for f in book_details.get("formats", []))
                    
                    if has_ebook and not self.ebook:
                        # This book is only available as an ebook, suggest using --ebook
                        raise BookHasNoAudiobook("book_ebook_only")
                    else:
                        # No audiobook or ebook format available
                        raise BookHasNoAudiobook("book_noaudio")
            # use API when possible
            elif list_type in ("series", "authors", "narrators"):
                return self.download_lists_api(url, list_type, language)
            # some lists are not avaialble via the API, use website scrapting
            else:
                return self.download_books_from_website(url)
        raise BookNotFound

    def download_book_from_url(self, url: str) -> Audiobook:
        consumableId = self.get_id_from_url(url)
        return self.download_book_from_book_id(consumableId)

    def download_lists_api(
        self,
        url: str,
        list_type: str,
        language: str,
    ) -> Series[str]:
        """
        Download and process books from list API endpoints
        
        :param url: URL of the list
        :param list_type: Type of list (series, authors, narrators)
        :param language: Language code
        :returns: Series object containing book ids
        """
        list_id: str = self.get_id_from_url(url)
        
        # Set format based on ebook flag
        formats = "ebook" if self.ebook else "abook"
        
        # List of languages to try - start with the original language, then try common alternatives
        languages_to_try = [language]  # Start with original language
        
        # Add fallback languages if not already in the list
        fallback_languages = ['en', 'us', 'uk', 'nl']
        for lang in fallback_languages:
            if lang != language and lang not in languages_to_try:
                languages_to_try.append(lang)
        
        # Keep track of which language works
        successful_language = None
        list_details = None
        all_items = []
        
        # Try each language until one works
        for current_language in languages_to_try:
            logging.log(f"Trying language: {current_language} for list ID: {list_id}")
            
            try:
                # Initialize variables for pagination
                nextPageToken = None
                items_for_language = []
                language_details = None
                
                # Fetch all pages for this language
                while True:
                    params: dict[str, str] = {
                        "includeListDetails": "true",
                        "includeFormats": formats,
                        "includeLanguages": current_language,
                        "kidsMode": "false",
                    }
                    
                    # Add nextPageToken for subsequent requests
                    if nextPageToken is not None:
                        params["nextPageToken"] = nextPageToken
        
                    logging.log(f"Requesting with params: {params}")
                    
                    resp = self._session.get(
                        f"https://api.storytel.net/explore/lists/{list_type}/{list_id}",
                        params=params,
                    )
        
                    logging.log(f"Response status: {resp.status_code}")
                    
                    # Skip this language if we get a 404
                    if resp.status_code == 404:
                        logging.log(f"List not found for language: {current_language}, trying next language")
                        break
                        
                    # Handle other non-200 responses
                    if resp.status_code != 200:
                        logging.log(f"Error response for language {current_language}: {resp.status_code}")
                        break
                    
                    data = resp.json()
                    
                    # Debug response
                    if "items" in data:
                        item_count = len(data["items"])
                        logging.log(f"Found {item_count} items for language: {current_language}")
                        
                        # If we have items, mark this language as successful
                        if item_count > 0:
                            successful_language = current_language
                    else:
                        logging.log(f"No items found for language: {current_language}")
                    
                    # Store first response to keep structure
                    if language_details is None:
                        language_details = data
                    
                    # Add items from this page
                    items_for_language.extend(data.get("items", []))
                    
                    # Get token for next page
                    nextPageToken = data.get("nextPageToken")
                    
                    # Exit loop if no more pages
                    if nextPageToken is None:
                        break
                
                # If we found items for this language, save them
                if len(items_for_language) > 0:
                    if list_details is None:
                        list_details = language_details
                    all_items = items_for_language
                    logging.log(f"Successfully found {len(all_items)} items with language: {current_language}")
                    break  # Stop trying other languages
                    
            except Exception as e:
                logging.log(f"Error processing language {current_language}: {str(e)}")
                continue
        
        # If we didn't find any valid language, return empty series
        if list_details is None:
            logging.log(f"No results found for any language. Tried: {', '.join(languages_to_try)}")
            return Series(
                title=f"The {list_type.capitalize()} (ID: {list_id})",
                books=[],
            )
        
        # Combine all items into the result
        list_details["items"] = all_items
        
        # Save to disk
        lists_path = self._get_lists_path(list_details.get("id", list_id), successful_language or language, formats)
        with open(lists_path, "w") as json_file:
            json_data = json.dumps(list_details, indent=2)
            json_file.write(json_data)
        
        # Process items to extract book IDs
        books: List[Union[BookId[str], Audiobook]] = []
        for item in list_details["items"]:
            format_type = "ebook" if self.ebook else "abook"
            
            # Check if formats key exists
            if "formats" not in item:
                logging.log(f"Item has no formats key: {item.get('id', 'unknown')}")
                continue
                
            abook_formats = [
                format for format in item["formats"] if format["type"] == format_type
            ]
            
            if len(abook_formats) > 0:
                if "isReleased" in abook_formats[0] and not abook_formats[0]["isReleased"]:
                    logging.log(f"Skipping unreleased item: {item.get('id', 'unknown')}")
                    continue
                
                if "id" not in item:
                    logging.log(f"Item has no id field")
                    continue
                    
                skip_check = self._skip_download_check(item["id"])
                if skip_check:
                    logging.log(f"Skipping already downloaded item: {item['id']}")
                    continue
                    
                book_id = BookId(item["id"])
                books.append(book_id)
                logging.log(f"Added item with id {item['id']} to download queue")

        # Determine the title
        series_title = (
            list_details.get("title") or 
            list_details.get("name") or 
            list_details.get("listMetadata", {}).get("title")
        )
        
        # If we still don't have a title, extract it from the URL
        if not series_title:
            try:
                parsed = parse_url(url)
                if parsed.path:
                    path_parts = parsed.path.split('/')
                    if len(path_parts) >= 2:
                        raw_title = path_parts[-1].split('-')[0:-1]
                        series_title = ' '.join(raw_title).title()
            except:
                series_title = f"{list_type.capitalize()} {list_id}"

        logging.log(f"Final book count: {len(books)}")
        logging.log(f"Series title: {series_title}")
        
        # If we succeeded with a different language, update the original URL
        if successful_language and successful_language != language:
            # Parse the original URL to get its components
            parsed = parse_url(url)
            path_parts = parsed.path.split('/')
            
            # Replace the language part in the path
            for i, part in enumerate(path_parts):
                if part == language:
                    path_parts[i] = successful_language
                    break
            
            # Reconstruct the path
            new_path = '/'.join(path_parts)
            
            # Create a new URL with the updated language
            scheme = parsed.scheme or 'https'
            netloc = parsed.netloc or 'www.storytel.com'
            
            new_url = f"{scheme}://{netloc}{new_path}"
            logging.log(f"Language mismatch detected. Try using this URL instead: {new_url}")

        return Series(
            title=series_title,
            books=books,
        )

    def download_book_from_book_id(
        self,
        consumableId: str,
    ) -> Audiobook:
        try:
            book_details = self.download_book_details(consumableId)
            
            # Debug: print book details when debug mode is on
            if self.debug:
                logging.debug(f"Book details: {json.dumps(book_details, indent=2, default=str)}")
            
            metadata = self.get_metadata(book_details)
            files = self.get_files(book_details)
            cover = self.download_cover(book_details)
            chapters = self.get_chapters(book_details)
            self._update_metadata(consumableId, book_details, metadata, files)

            return Audiobook(
                session=self._session,
                files=files,
                metadata=metadata,
                cover=cover,
                chapters=chapters,
                source_data=book_details,
            )
        except BookHasNoAudiobook as e:
            # Use a simple error code instead of the full message to avoid filename issues
            if self.ebook is None and any(f["type"] == "ebook" for f in book_details.get("formats", [])):
                raise BookHasNoAudiobook("book_ebook_only")
            else:
                raise BookHasNoAudiobook("book_noaudio")
        except Exception as e:
            if self.debug:
                logging.debug(f"Error in download_book_from_book_id: {str(e)}")
                logging.debug(traceback.format_exc())
            raise

    @staticmethod
    def get_id_from_url(url: str) -> str:
        parsed = parse_url(url)
        if parsed.path is None:
            raise DataNotPresent
        return parsed.path.split("-")[-1]

    @staticmethod
    def _update_metadata(
        consumableId: str,
        book_details: Dict[str, Any],
        metadata: AudiobookMetadata,
        files: List[AudiobookFile],
    ) -> None:
        # The ISBN is only available from the download link
        parsed = parse_url(files[0].url)
        q = parse_qs(parsed.query)
        if "isbn" in q:
            isbn = q["isbn"][0]
            book_details["_download_url_isbn"] = isbn
            metadata.isbn = isbn
        if consumableId in metadata_corrections["books"]:
            corrections = metadata_corrections["books"][consumableId]
            for key, value in corrections.items():
                logging.log(
                    f"overriding metadata [yellow]{key}[/] from [blue]{getattr(metadata, key)}[/] to [magenta]{value}[/]"
                )
                setattr(metadata, key, value)

    def download_bookshelf(self) -> Dict[str, Any]:
        resp = self._session.post(
            "https://api.storytel.net/libraries/bookshelf",
            json={"items": []},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        data: Dict[str, Any] = resp.json()

        bookshelf_path = os.path.join(self.database_directory_lists, f"bookshelf.json")
        with open(bookshelf_path, "w") as json_file:
            json_data = json.dumps(data, indent=2)
            json_file.write(json_data)

        return data

    def download_books_from_website(self, url: str) -> Series[str]:
        title = self.find_elems_in_page(url, "h1")[-1].text
        items = self.find_elems_in_page(url, 'a[href*="/books/"]')
        books: List[Union[BookId[str], Audiobook]] = []
        for item in items:
            href: str = item.get("href")
            # check for headphone icon to filter out non audiobooks
            svg_headphone_element = item.cssselect(
                f"svg > path[d='{svg_headphone_path}']"
            )
            if len(svg_headphone_element) == 0 and not self.ebook:
                logging.debug(f"skipping {href} (has no audiobook)")
                continue

            consumableId = self.get_id_from_url(href)
            if not self._skip_download_check(consumableId):
                book_id = BookId(consumableId)
                books.append(book_id)

        return Series(
            title=title,
            books=books,
        )

    def download_book_details(self, consumableId: str) -> Dict[str, Any]:
        resp = self._session.get(
            f"https://api.storytel.net/book-details/consumables/{consumableId}?kidsMode=false&configVariant=default"
        )
        if resp.status_code == 404:
            raise BookNotFound
        data = resp.json()
        return data

    def get_audio_url(self, consumableId: str) -> str:
        consumable_type = "ebook" if self.ebook else "abook"
        resp = self._session.get(
            f"https://api.storytel.net/assets/v2/consumables/{consumableId}/{consumable_type}",
            allow_redirects=False,
        )
        self._download_counter += 1
        if resp.status_code != 302:
            raise GenericAudiobookDLException(
                f"request to {resp.url} failed, got {resp.status_code} response: {resp.text}"
            )
        location: str = resp.headers["Location"]
        return location

    def get_files(self, book_info) -> List[AudiobookFile]:
        consumableId = book_info["consumableId"]
        audio_url = self.get_audio_url(consumableId)

        return [
            AudiobookFile(
                url=audio_url,
                headers=self._session.headers,
                ext="epub" if self.ebook else "mp3",
                expected_status_code=200,
                expected_content_type="application/epub+zip" if self.ebook else "audio/mpeg",
            )
        ]

    def get_metadata(self, book_details) -> AudiobookMetadata:
        title = book_details["title"]
        metadata = AudiobookMetadata(title)
        metadata.add_genre("Audiobook")
        metadata.scrape_url = self._clean_share_url(book_details["shareUrl"])
        
        if self.debug:
            logging.debug(f"URL {metadata.scrape_url}")
            logging.debug(f"Book formats: {json.dumps(book_details.get('formats', []), indent=2, default=str)}")
        
        for author in book_details["authors"]:
            metadata.add_author(author["name"])
        for narrator in book_details["narrators"]:
            metadata.add_narrator(narrator["name"])
        if "isbn" in book_details:
            if book_details["isbn"]:
                metadata.isbn = book_details["isbn"]
        if "description" in book_details:
            metadata.description = book_details["description"]
        if "language" in book_details:
            if book_details["language"]:
                metadata.language = pycountry.languages.get(
                    alpha_2=book_details["language"]
                )
        if "category" in book_details:
            if "name" in book_details["category"]:
                metadata.add_genre(book_details["category"]["name"])
        if "seriesInfo" in book_details and book_details["seriesInfo"]:
            metadata.series = book_details["seriesInfo"]["name"]
            if "orderInSeries" in book_details["seriesInfo"]:
                metadata.series_order = book_details["seriesInfo"]["orderInSeries"]

        # Check format type (ebook or abook)
        format_type = "ebook" if self.ebook else "abook"
        
        # Check if formats exist in book_details
        if not "formats" in book_details or not book_details["formats"]:
            if self.debug:
                logging.debug("No formats found in book_details")
            # Try to fetch the format from the playback metadata if looking for audiobook
            if not self.ebook:
                try:
                    playback_metadata = self.download_audiobook_info(book_details)
                    if self.debug:
                        logging.debug(f"Playback metadata: {playback_metadata}")
                    # If we get here, we found audio content
                except Exception as e:
                    if self.debug:
                        logging.debug(f"Error fetching playback metadata: {str(e)}")
                    # If we can't get audiobook information, raise the exception with simple code
                    raise BookHasNoAudiobook("book_noaudio")
            else:
                # If we're looking for an ebook but no formats found
                raise BookHasNoAudiobook("book_noebook")
        else:
            # Check for the desired format (ebook or abook)
            desired_formats = [f for f in book_details["formats"] if f["type"] == format_type]
            
            if len(desired_formats) == 0:
                other_format = "abook" if self.ebook else "ebook"
                other_formats = [f for f in book_details["formats"] if f["type"] == other_format]
                
                if self.debug:
                    if len(other_formats) > 0:
                        logging.debug(f"No {format_type} format found, but {other_format} is available")
                    else:
                        logging.debug(f"No {format_type} format found and no {other_format} available either")
                
                # Raise appropriate exception based on what we're looking for
                if self.ebook:
                    raise BookHasNoAudiobook("book_noebook")
                else:
                    raise BookHasNoAudiobook("book_noaudio")
            
            format = desired_formats[0]
            
            if "isReleased" in format and not format["isReleased"]:
                raise BookNotReleased
                
            if "publisher" in format and "name" in format["publisher"]:
                metadata.publisher = format["publisher"]["name"]
                
            if "releaseDate" in format:
                date_str: str = format["releaseDate"]
                try:
                    metadata.release_date = datetime.strptime(
                        date_str, "%Y-%m-%dT%H:%M:%SZ"
                    ).date()
                except ValueError:
                    if self.debug:
                        logging.debug(f"Error parsing date: {date_str}")
                    # Try alternative date formats
                    try:
                        metadata.release_date = datetime.fromisoformat(date_str).date()
                    except ValueError:
                        if self.debug:
                            logging.debug(f"Failed to parse date with alternative format: {date_str}")
        return metadata

    def download_audiobook_info(self, book_details) -> Dict[str, Any]:
        consumableId = book_details["consumableId"]
        url = f"https://api.storytel.net/playback-metadata/consumable/{consumableId}"
        response = self._session.get(url)
        
        if response.status_code != 200:
            if self.debug:
                logging.debug(f"Failed to get playback metadata: {response.status_code} - {response.text}")
            raise DataNotPresent
            
        playback_metadata = response.json()
        playback_metadata_path = self._get_playback_metadata_path(consumableId)
        with open(playback_metadata_path, "w") as json_file:
            json_data = json.dumps(playback_metadata, indent=2)
            json_file.write(json_data)
            
        if not "formats" in playback_metadata:
            raise DataNotPresent
            
        for format in playback_metadata["formats"]:
            if format["type"] == "abook":
                return format
                
        raise DataNotPresent

    def get_chapters(self, book_details) -> List[Chapter]:
        chapters: List[Chapter] = []
        
        # Skip chapter processing for ebooks
        if self.ebook:
            return chapters
            
        book_title = book_details["title"]
        
        try:
            file_metadata = self.download_audiobook_info(book_details)
        except Exception as e:
            if self.debug:
                logging.debug(f"Error getting chapters: {str(e)}")
            return []
            
        if not "chapters" in file_metadata:
            return []
            
        start_time = 0
        for chapter in file_metadata["chapters"]:
            if "title" in chapter and chapter["title"] is not None:
                title = chapter["title"]
                # remove book title prefix from chapter title
                if len(title) > len(book_title) and title.startswith(book_title):
                    title = title[len(book_title) :].strip(" -")
            else:
                title = f"Chapter {chapter['number']}"
            chapters.append(Chapter(start_time, title))
            start_time += chapter["durationInMilliseconds"]
        return chapters

    def download_cover(self, book_details) -> Cover:
        if "cover" not in book_details or "url" not in book_details["cover"]:
            # Return a placeholder cover if no cover is available
            logging.debug("No cover found for book")
            return Cover(b"", "jpg")
            
        cover_url = book_details["cover"]["url"]
        try:
            cover_data = self.get(cover_url)
            return Cover(cover_data, "jpg")
        except Exception as e:
            if self.debug:
                logging.debug(f"Error downloading cover: {str(e)}")
            # Return empty cover on error
            return Cover(b"", "jpg")

    def on_download_complete(self, audiobook: Audiobook) -> None:
        consumableId = audiobook.source_data["consumableId"]
        book_path = self._get_book_path(consumableId)
        with open(book_path, "w") as json_file:
            json_data = json.dumps(audiobook.source_data, indent=2)
            json_file.write(json_data)