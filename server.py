import os
import http.server
import socketserver
import datetime
import xml.etree.ElementTree as ET
import hashlib
from urllib.parse import unquote, quote, urlparse, parse_qs
from ebooklib import epub

LIBRARY_DIR = os.environ.get('LIBRARY_DIR', 'books')
PORT = 8080
PAGE_SIZE = int(os.environ.get('PAGE_SIZE', 25))


class BookMetadata:
    @staticmethod
    def extract_epub_metadata(epub_path):
        try:
            book = epub.read_epub(epub_path)
            title = book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else None
            author = book.get_metadata('DC', 'creator')[0][0] if book.get_metadata('DC', 'creator') else None
            return title, author
        except Exception:
            return None, None


class SecurityUtils:
    @staticmethod
    def is_within_library_dir(file_path):
        library_realpath = os.path.realpath(LIBRARY_DIR)
        file_realpath = os.path.realpath(file_path)
        return file_realpath.startswith(library_realpath + os.sep) or file_realpath == library_realpath
    
    @staticmethod
    def has_path_traversal(path):
        """Check if path contains dangerous traversal sequences"""
        dangerous_patterns = ['..', '~']
        
        for pattern in dangerous_patterns:
            if pattern in path:
                return True
        
        path_components = path.replace('\\', '/').split('/')
        for component in path_components:
            if component in dangerous_patterns or component.startswith('.'):
                return True
                
        return False


class OPDSFeedGenerator:
    @staticmethod
    def generate_feed(title, feed_id, links, entries):
        feed = ET.Element('feed', {
            'xmlns': 'http://www.w3.org/2005/Atom',
            'xmlns:opds': 'http://opds-spec.org/2010/catalog'
        })
        
        ET.SubElement(feed, 'title').text = title
        ET.SubElement(feed, 'id').text = feed_id
        ET.SubElement(feed, 'updated').text = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'
        
        for rel, href, type_ in links:
            ET.SubElement(feed, 'link', {'rel': rel, 'href': href, 'type': type_})
            
        for entry_data in entries:
            entry = ET.SubElement(feed, 'entry')
            ET.SubElement(entry, 'title').text = entry_data['title']
            ET.SubElement(entry, 'id').text = entry_data['id']
            
            if 'author' in entry_data:
                author = ET.SubElement(entry, 'author')
                ET.SubElement(author, 'name').text = entry_data['author']
                
            for rel, href, type_ in entry_data['links']:
                ET.SubElement(entry, 'link', {'rel': rel, 'href': href, 'type': type_})
                
        xml_string = ET.tostring(feed, encoding='unicode', method='xml')

        processing_instruction = '<?xml-stylesheet type="text/xsl" href="/opds_to_html.xslt"?>\n'
        return processing_instruction + xml_string


class BookScanner:
    def __init__(self):
        self.metadata_extractor = BookMetadata()
        self.security = SecurityUtils()
        self._all_books_cache = None
        self._recent_books_cache = None
        self._recent_books_cache_time = 0
        self.RECENT_CACHE_TTL = 300  # 5 minutes cache for recent books
    
    def scan_directory(self, directory_path, base_path=None):
        if base_path is None:
            base_path = directory_path
            
        file_list = []
        
        for root, dirs, files in os.walk(directory_path):
            for file in files:
                if file.endswith('.epub'):
                    file_info = self._create_file_info(root, file, base_path)
                    if file_info:
                        file_list.append(file_info)
                        
        return sorted(file_list, key=lambda x: x['title'])
    
    def scan_directory_single_level(self, directory_path, base_path=None):
        """Scans only the direct files in a directory (non-recursive)."""
        if base_path is None:
            base_path = directory_path
            
        file_list = []
        
        try:
            for file in os.listdir(directory_path):
                if file.endswith('.epub'):
                    file_path = os.path.join(directory_path, file)
                    if os.path.isfile(file_path):
                        file_info = self._create_file_info(directory_path, file, base_path)
                        if file_info:
                            file_list.append(file_info)
        except OSError:
            pass
                        
        return sorted(file_list, key=lambda x: x['title'])
    
    def scan_full_book_list(self):
        """Scans the entire library and caches the result."""
        if self._all_books_cache is None:
            self._all_books_cache = self.scan_directory(LIBRARY_DIR)
        return self._all_books_cache
    
    def get_all_books_paginated(self, page, size):
        """Get paginated view of all books with collections support."""
        # Get root level content (collections and loose books)
        collections = []
        loose_books = []
        
        try:
            for item in sorted(os.listdir(LIBRARY_DIR)):
                item_path = os.path.join(LIBRARY_DIR, item)
                if os.path.isdir(item_path) and not item.startswith('.'):
                    collections.append({
                        'type': 'collection',
                        'name': item,
                        'path': item
                    })
                elif item.endswith('.epub') and os.path.isfile(item_path):
                    file_info = self._create_file_info(LIBRARY_DIR, item, LIBRARY_DIR)
                    if file_info:
                        loose_books.append({
                            'type': 'book',
                            'info': file_info
                        })
        except OSError:
            pass
        
        # Combine collections and loose books
        all_items = collections + loose_books
        total_count = len(all_items)
        
        # Apply pagination
        start = (page - 1) * size
        end = start + size
        paginated_items = all_items[start:end]
        
        return paginated_items, total_count
        
    def get_paginated_books(self, directory_path, page, size, base_path=None, use_cache=False):
        """Returns a subset of books for a given page."""
        if use_cache:
            file_list = self.scan_full_book_list()
        else:
            file_list = self.scan_directory(directory_path, base_path=base_path)

        start = (page - 1) * size
        end = start + size
        
        return file_list[start:end], len(file_list)
    
    def get_folder_content_paginated(self, folder_full_path, parent_folder_path, page, size, base_path=None):
        """Returns paginated folder content (subfolders + books combined)."""
        # Get subfolders
        subfolders = []
        for item in os.listdir(folder_full_path):
            item_path = os.path.join(folder_full_path, item)
            if os.path.isdir(item_path) and not item.startswith('.'):
                subfolders.append(item)
        subfolders = sorted(subfolders)
        
        # Convert subfolders to entries
        subfolder_entries = []
        for subfolder in subfolders:
            subfolder_relative = os.path.join(parent_folder_path, subfolder)
            subfolder_id = f'urn:folder:{hashlib.md5(subfolder_relative.encode()).hexdigest()}'
            encoded_subfolder = quote(subfolder_relative.replace(os.sep, '/'))
            
            subfolder_entries.append({
                'title': subfolder,
                'id': subfolder_id,
                'type': 'folder',
                'links': [('subsection', f'/opds/folder/{encoded_subfolder}?page=1', 'application/atom+xml;profile=opds-catalog;kind=acquisition')]
            })
        
        # Get books
        book_list = self.scan_directory(folder_full_path, base_path=base_path)
        book_entries = []
        for file_info in book_list:
            book_id = f'urn:book:{hashlib.md5(file_info["relative_path"].encode()).hexdigest()}'
            encoded_path = quote(file_info['relative_path'].replace(os.sep, '/'))
            
            book_entries.append({
                'title': file_info['title'],
                'id': book_id,
                'type': 'book',
                'author': file_info['author'],
                'links': [('http://opds-spec.org/acquisition/open-access', f'/download/{encoded_path}', 'application/epub+zip')]
            })
        
        # Combine all entries
        all_entries = subfolder_entries + book_entries
        total_count = len(all_entries)
        
        # Apply pagination
        start = (page - 1) * size
        end = start + size
        paginated_entries = all_entries[start:end]
        
        return paginated_entries, total_count
    
    def scan_recent_books(self, directory_path, limit=10):
        """Fast recent books scan using file modification times without full directory traversal."""
        import time
        
        # Check cache first
        current_time = time.time()
        if (self._recent_books_cache is not None and 
            current_time - self._recent_books_cache_time < self.RECENT_CACHE_TTL):
            return self._recent_books_cache[:limit]
        
        recent_files = []
        
        def scan_for_recent_files(path, current_depth=0, max_depth=3):
            """Recursively scan for recent files with depth limit."""
            if current_depth > max_depth:
                return
                
            try:
                entries = []
                for entry in os.scandir(path):
                    if entry.is_file() and entry.name.endswith('.epub'):
                        try:
                            stat_info = entry.stat()
                            entries.append((entry.path, stat_info.st_mtime, entry.name))
                        except OSError:
                            continue
                    elif entry.is_dir() and not entry.name.startswith('.'):
                        scan_for_recent_files(entry.path, current_depth + 1, max_depth)
                
                # Sort by modification time and keep only the most recent files from this directory
                entries.sort(key=lambda x: x[1], reverse=True)
                recent_files.extend(entries[:limit * 2])  # Get more than needed for better selection
                
            except OSError:
                pass
        
        scan_for_recent_files(directory_path)
        
        # Sort all found files by modification time and take the top ones
        recent_files.sort(key=lambda x: x[1], reverse=True)
        recent_files = recent_files[:limit * 2]  # Get more candidates
        
        # Convert to file_info objects
        file_list = []
        for file_path, mtime, filename in recent_files:
            if len(file_list) >= limit:
                break
                
            # Quick security check
            relative_path = os.path.relpath(file_path, directory_path)
            if (not self.security.has_path_traversal(relative_path) and 
                self.security.is_within_library_dir(file_path)):
                
                # Extract metadata for recent files
                title, author = self.metadata_extractor.extract_epub_metadata(file_path)
                title = title or filename
                author = author or 'Unknown'
                
                file_list.append({
                    'path': file_path,
                    'relative_path': relative_path,
                    'title': title,
                    'author': author,
                    'mtime': mtime
                })
        
        # Cache the results
        self._recent_books_cache = file_list
        self._recent_books_cache_time = current_time
        
        return file_list
    
    def _create_file_info(self, root, filename, base_path):
        path = os.path.join(root, filename)
        relative_path = os.path.relpath(path, base_path)
        
        if self.security.has_path_traversal(relative_path) or not self.security.is_within_library_dir(path):
             return None
        
        title, author = self.metadata_extractor.extract_epub_metadata(path)
        title = title or filename
        author = author or 'Unknown'
        
        return {
            'path': path,
            'relative_path': relative_path,
            'title': title,
            'author': author,
            'mtime': os.path.getmtime(path)
        }


class OPDSHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.feed_generator = OPDSFeedGenerator()
        self.book_scanner = BookScanner()
        self.security = SecurityUtils()
        super().__init__(*args, **kwargs)
        
    def _parse_url_params(self):
        """Extracts and validates page and size from the URL query."""
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query)
        
        try:
            page = int(query_params.get('page', ['1'])[0])
            page = max(1, page)
        except ValueError:
            page = 1
            
        size = PAGE_SIZE
        
        return page, size, parsed_url

    def do_GET(self):
        if self.path == '/':
            self.send_response(302)
            self.send_header('Location', '/opds')
            self.end_headers()
        elif self.path == '/opds_to_html.xslt':
            self._serve_xslt()
        elif self.path.startswith('/opds'):
            path_base = urlparse(self.path).path
            if path_base == '/opds' or path_base == '/opds/':
                self._handle_root_catalog()
            elif path_base == '/opds/books':
                self._handle_all_books()
            elif path_base == '/opds/recent':
                self._handle_recent_books()
            elif path_base.startswith('/opds/folder/'):
                self._handle_folder_catalog()
            else:
                self._send_error(404, 'OPDS Catalog Not found')
        elif self.path.startswith('/download/'):
            self._handle_download()
        else:
            self._send_error(404, 'Not found')
    
    def _handle_root_catalog(self):
        links = [
            ('self', '/opds', 'application/atom+xml;profile=opds-catalog;kind=navigation'),
            ('start', '/opds', 'application/atom+xml;profile=opds-catalog;kind=navigation')
        ]
        
        entries = self._get_root_entries()
        xml = self.feed_generator.generate_feed('My Library', 'urn:library-root', links, entries)
        
        self._send_xml_response(xml, 'navigation')
    
    def _get_root_entries(self):
        entries = [
            {
                'title': 'All Books',
                'id': 'urn:all-books',
                'links': [('subsection', '/opds/books?page=1', 'application/atom+xml;profile=opds-catalog;kind=acquisition')]
            },
            {
                'title': 'Recent Books',
                'id': 'urn:recent-books',
                'links': [('subsection', '/opds/recent', 'application/atom+xml;profile=opds-catalog;kind=acquisition')]
            }
        ]
        
        for folder in sorted(os.listdir(LIBRARY_DIR)):
            folder_path = os.path.join(LIBRARY_DIR, folder)
            if os.path.isdir(folder_path):
                folder_id = f'urn:folder:{hashlib.md5(folder.encode()).hexdigest()}'
                encoded_folder = quote(folder)
                entries.append({
                    'title': folder,
                    'id': folder_id,
                    'links': [('subsection', f'/opds/folder/{encoded_folder}?page=1', 'application/atom+xml;profile=opds-catalog;kind=acquisition')]
                })
                
        return entries
    
    def _handle_all_books(self):
        page, size, parsed_url = self._parse_url_params()
        path_base = parsed_url.path
        
        # Use the new efficient paginated method that supports collections
        paginated_items, total_count = self.book_scanner.get_all_books_paginated(page, size)
        
        links = self._get_pagination_links(path_base, page, size, total_count)
        links.append(('start', '/opds', 'application/atom+xml;profile=opds-catalog;kind=navigation'))
        
        # Convert items to entries
        entries = []
        for item in paginated_items:
            if item['type'] == 'collection':
                # Collection entry
                collection_id = f'urn:collection:{hashlib.md5(item["name"].encode()).hexdigest()}'
                encoded_name = quote(item['name'])
                entries.append({
                    'title': f"📁 {item['name']}",
                    'id': collection_id,
                    'links': [('subsection', f'/opds/folder/{encoded_name}?page=1', 'application/atom+xml;profile=opds-catalog;kind=acquisition')]
                })
            else:  # book
                # Book entry
                file_info = item['info']
                book_id = f'urn:book:{hashlib.md5(file_info["relative_path"].encode()).hexdigest()}'
                encoded_path = quote(file_info['relative_path'].replace(os.sep, '/'))
                
                entries.append({
                    'title': file_info['title'],
                    'id': book_id,
                    'links': [('http://opds-spec.org/acquisition/open-access', f'/download/{encoded_path}', 'application/epub+zip')],
                    'author': file_info['author']
                })
        
        total_pages = self._get_total_pages(total_count, size)
        title = f'All Books (Page {page} of {total_pages})'
        xml = self.feed_generator.generate_feed(title, 'urn:all-books', links, entries)
        
        self._send_xml_response(xml, 'acquisition')
    
    def _handle_recent_books(self):
        links = [
            ('self', '/opds/recent', 'application/atom+xml;profile=opds-catalog;kind=acquisition'),
            ('start', '/opds', 'application/atom+xml;profile=opds-catalog;kind=navigation')
        ]
        
        file_list = self.book_scanner.scan_recent_books(LIBRARY_DIR, limit=10)
        entries = self._create_book_entries(file_list)
        xml = self.feed_generator.generate_feed('Recent Books', 'urn:recent-books', links, entries)
        
        self._send_xml_response(xml, 'acquisition')
    
    def _handle_folder_catalog(self):
        page, size, parsed_url = self._parse_url_params()
        
        folder_path = unquote(parsed_url.path[len('/opds/folder/'):])
        path_base = parsed_url.path
        folder_full_path = os.path.join(LIBRARY_DIR, folder_path)
        
        if not self._validate_folder_access(folder_full_path):
            return
        
        # Use the new combined pagination method
        paginated_entries, total_count = self.book_scanner.get_folder_content_paginated(
            folder_full_path, folder_path, page, size, base_path=LIBRARY_DIR
        )
        
        # Convert entries to the format expected by the feed generator
        formatted_entries = []
        for entry in paginated_entries:
            if entry.get('type') == 'folder':
                formatted_entries.append({
                    'title': entry['title'],
                    'id': entry['id'],
                    'links': entry['links']
                })
            else:  # book
                formatted_entries.append({
                    'title': entry['title'],
                    'id': entry['id'],
                    'links': entry['links'],
                    'author': entry['author']
                })
        
        links = self._get_pagination_links(path_base, page, size, total_count)
        links.append(('start', '/opds', 'application/atom+xml;profile=opds-catalog;kind=navigation'))
        
        feed_id = f'urn:folder:{hashlib.md5(folder_path.encode()).hexdigest()}'
        title = os.path.basename(folder_path) or 'Library'
        
        total_pages = self._get_total_pages(total_count, size)
        if total_pages > 1:
            title = f'{title} (Page {page} of {total_pages})'
            
        xml = self.feed_generator.generate_feed(title, feed_id, links, formatted_entries)
        
        self._send_xml_response(xml, 'acquisition')

    def _get_total_pages(self, total_count, size=None):
        """Calculates the total number of pages."""
        if size is None:
            size = PAGE_SIZE
        return max(1, (total_count + size - 1) // size)
        
    def _get_pagination_links(self, path_base, current_page, size, total_count):
        """Generates all necessary OPDS paging links."""
        links = []
        
        total_pages = self._get_total_pages(total_count, size)
        
        links.append(('self', f'{path_base}?page={current_page}', 'application/atom+xml;profile=opds-catalog;kind=acquisition'))

        if total_pages > 1:
             links.append(('first', f'{path_base}?page=1', 'application/atom+xml;profile=opds-catalog;kind=acquisition'))
        
        if current_page < total_pages:
            links.append(('next', f'{path_base}?page={current_page + 1}', 'application/atom+xml;profile=opds-catalog;kind=acquisition'))
            
        if current_page > 1:
            links.append(('previous', f'{path_base}?page={current_page - 1}', 'application/atom+xml;profile=opds-catalog;kind=acquisition'))

        if total_pages > 1:
            links.append(('last', f'{path_base}?page={total_pages}', 'application/atom+xml;profile=opds-catalog;kind=acquisition'))
            
        return links

    def _validate_folder_access(self, folder_full_path):
        if not self.security.is_within_library_dir(folder_full_path) or not os.path.isdir(folder_full_path):
            self._send_error(404, 'Folder not found or access denied')
            return False
        return True
    
    def _create_book_entries(self, file_list):
        entries = []
        for file_info in file_list:
            book_id = f'urn:book:{hashlib.md5(file_info["relative_path"].encode()).hexdigest()}'
            encoded_path = quote(file_info['relative_path'].replace(os.sep, '/'))
            
            entry_links = [
                ('http://opds-spec.org/acquisition/open-access', f'/download/{encoded_path}', 'application/epub+zip')
            ]
            
            entries.append({
                'title': file_info['title'],
                'id': book_id,
                'links': entry_links,
                'author': file_info['author']
            })
            
        return entries
    
    def _handle_download(self):
        filename = unquote(self.path.split('/download/')[1])
        
        if self.security.has_path_traversal(filename):
            self._send_error(403, 'Access denied: Invalid path')
            return
        
        file_path = os.path.join(LIBRARY_DIR, filename)
        
        if not self.security.is_within_library_dir(file_path):
            self._send_error(403, 'Access denied: Path traversal detected')
            return
            
        if self._is_valid_epub_file(filename, file_path):
            self._serve_file(file_path, filename)
        else:
            self._send_error(404, 'File not found')
    
    def _is_valid_epub_file(self, filename, file_path):
        return filename.endswith('.epub') and os.path.exists(file_path)
    
    def _serve_file(self, file_path, filename):
        self.send_response(200)
        self.send_header('Content-Type', 'application/epub+zip')
        self.send_header('Content-Disposition', f'attachment; filename="{os.path.basename(filename)}"')
        self.end_headers()
        
        with open(file_path, 'rb') as f:
            self.wfile.write(f.read())
    
    def _serve_xslt(self):
        try:
            xslt_path = 'opds_to_html.xslt'
            if not os.path.exists(xslt_path):
                self._send_error(404, "XSLT file not found")
                return

            self.send_response(200)
            self.send_header('Content-Type', 'application/xml')
            self.end_headers()
            with open(xslt_path, 'rb') as f:
                self.wfile.write(f.read())
        except Exception as e:
            self._send_error(500, f"Error serving XSLT file: {e}")

    def _send_xml_response(self, xml, catalog_kind):
        self.send_response(200)
        self.send_header('Content-Type', f'application/xml;profile=opds-catalog;kind={catalog_kind}')
        self.end_headers()
        self.wfile.write(xml.encode('utf-8'))

    def _send_error(self, code, message):
        self.send_response(code)
        self.send_header('Content-Type', 'application/xml')
        self.end_headers()
        error_xml = f'<?xml version="1.0" encoding="UTF-8"?><error><code>{code}</code><message>{message}</message></error>'
        self.wfile.write(error_xml.encode('utf-8'))


def main():
    if not os.path.exists(LIBRARY_DIR):
        os.makedirs(LIBRARY_DIR)
        
    with socketserver.TCPServer(("", PORT), OPDSHandler) as httpd:
        print(f"OPDS server started on port {PORT}")
        print(f"Access the root catalog at http://127.0.0.1:{PORT}/opds")
        httpd.serve_forever()


if __name__ == '__main__':
    main()