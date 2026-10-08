"""Offline discovery contract; browser rendering and hosted responses are separate gates."""
import ast
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import unittest
from urllib.parse import urlsplit
import zipfile

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / 'apps/photocraft-web'
ORIGIN = 'https://photocraft-studio-d42c446ec275.trytofu.app'


class Html(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = []
        self.words = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.nodes.append((tag, attrs, tuple(self.stack)))
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append((tag, attrs.get('id')))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.words.append((data, tuple(self.stack)))

    def attrs(self, tag):
        return [attrs for kind, attrs, _ in self.nodes if kind == tag]

    def text(self, tag):
        return ' '.join(text.strip() for text, parents in self.words
                        if any(kind == tag for kind, _ in parents) and text.strip())


class Discovery(unittest.TestCase):
    def setUp(self):
        self.app_source = (WEB / 'index.html').read_text()
        self.about_source = (WEB / 'about.html').read_text()
        self.app = Html(self.app_source)
        self.about = Html(self.about_source)

    def metadata(self, document):
        values = {}
        for attrs in document.attrs('meta'):
            key = attrs.get('name', attrs.get('property'))
            if key:
                self.assertNotIn(key, values, 'Conflicting duplicate metadata')
                values[key] = attrs.get('content', '')
        return values

    def test_public_content_is_readable_without_editor_or_javascript(self):
        self.assertEqual(self.about.attrs('html'), [{'lang': 'en'}])
        self.assertEqual(len(self.about.attrs('h1')), 1)
        self.assertGreater(len(self.about.text('main').split()), 150)
        self.assertIn('PhotoCraft Studio', self.about.text('title'))
        self.assertIn('ArtCraft team', self.about.text('main'))
        self.assertIn('independent', self.about.text('main'))
        self.assertIn('early-alpha', self.about.text('main'))
        self.assertIn('collaborative undo and unrestricted simultaneous editing are not available', self.about.text('main'))
        for tag in ('script', 'canvas', 'iframe', 'object', 'embed', 'form', 'base'):
            self.assertFalse(self.about.attrs(tag), f'About must not bootstrap {tag}')
        for _, attrs, _ in self.about.nodes:
            self.assertFalse(any(key.startswith('on') for key in attrs), 'No inline script handlers')
            for key in ('src', 'href'):
                if key in attrs:
                    self.assertNotEqual(urlsplit(attrs[key]).scheme, 'javascript')
        # All resources stay local; ordinary outbound links load only on activation.
        self.assertEqual([a['src'] for a in self.about.attrs('img')], ['/media/editor.png'])
        self.assertFalse([a for a in self.about.attrs('link') if a.get('rel') != 'canonical'])
        self.assertNotRegex(self.about.text('style'), r'(?i)@import|url\s*\(')
        hrefs = {a.get('href') for a in self.about.attrs('a')}
        self.assertTrue({'/', 'https://trytofu.ai/', 'https://trytofu.ai/docs',
                         'https://github.com/storytold/photocraft',
                         'https://github.com/airisorg/photocraft-studio#readme'} <= hrefs)
        self.assertFalse({'/auth/login', '/api/config', '/api/me'} & hrefs)

    def test_index_policy_and_social_cards_are_generic_initial_html(self):
        for document, path, policy in ((self.about, '/about.html', {'index', 'follow'}),
                                       (self.app, '/', {'noindex', 'follow'})):
            with self.subTest(path=path):
                metadata = self.metadata(document)
                self.assertEqual({part.strip() for part in metadata['robots'].split(',')}, policy)
                self.assertGreater(len(metadata['description']), 60)
                self.assertEqual(metadata['og:type'], 'website')
                self.assertEqual(metadata['og:url'], ORIGIN + path)
                self.assertEqual(metadata['og:image'], ORIGIN + '/media/social-preview.png')
                self.assertEqual(metadata['og:image:type'], 'image/png')
                self.assertEqual((metadata['og:image:width'], metadata['og:image:height']), ('1280', '640'))
                self.assertGreater(len(metadata['og:image:alt']), 20)
                for key, value in metadata.items():
                    self.assertNotRegex(value, r'(?i)\?share=|\?project=|access_token|aggregateRating|reviewCount', key)
        self.assertEqual(self.about.attrs('link'), [{'rel': 'canonical', 'href': ORIGIN + '/about.html'}])

    def test_about_link_does_not_add_an_editor_overlay(self):
        links = [(attrs, parents) for tag, attrs, parents in self.app.nodes
                 if tag == 'a' and attrs.get('href') == '/about.html']
        self.assertEqual(len(links), 2)
        self.assertTrue(any(('div', 'photocraft_loading') in parents for _, parents in links))
        self.assertTrue(any(('noscript', None) in parents for _, parents in links))
        for _, parents in links:
            self.assertTrue(('div', 'photocraft_loading') in parents or ('noscript', None) in parents)
        self.assertEqual([a['id'] for a in self.app.attrs('canvas')], ['photocraft_canvas'])
        rust = [a for a in self.app.attrs('link') if a.get('rel') == 'rust']
        self.assertEqual(len(rust), 1)
        self.assertEqual(rust[0]['data-bin'], 'photocraft-web')
        self.assertIn('heif', rust[0]['data-cargo-features'])

    def copied_files(self):
        # Trunk v0.21.14 copy-file preserves basename; target-path is a directory:
        # https://github.com/trunk-rs/trunk/blob/v0.21.14/src/pipelines/copy_file.rs
        copied = {}
        for attrs in self.app.attrs('link'):
            if attrs.get('rel') != 'copy-file':
                continue
            self.assertIn('data-trunk', attrs)
            source = (WEB / attrs['href']).resolve()
            self.assertTrue(source.is_relative_to(ROOT))
            target = PurePosixPath(attrs.get('data-target-path', '.')) / source.name
            self.assertFalse(target.is_absolute())
            self.assertNotIn('..', target.parts)
            self.assertNotIn(target.as_posix(), copied)
            copied[target.as_posix()] = source
        return copied

    def test_trunk_media_paths_and_real_image_dimensions(self):
        copied = self.copied_files()
        self.assertEqual(copied, {
            'about.html': WEB / 'about.html',
            'media/editor.png': ROOT / 'docs/media/editor.png',
            'media/social-preview.png': ROOT / 'docs/media/social-preview.png',
        })
        for name, size in (('editor.png', (1440, 960)), ('social-preview.png', (1280, 640))):
            with self.subTest(image=name), Image.open(copied['media/' + name]) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.size, size)
                image.verify()
        self.assertEqual((self.about.attrs('img')[0]['width'], self.about.attrs('img')[0]['height']), ('1440', '960'))

    def test_real_packager_asset_loop_keeps_about_and_image_bytes(self):
        # Execute the existing production asset loop on a disposable dist fixture.
        # This does not substitute for a Trunk build or the full notice/package gate.
        path = ROOT / 'packaging/web/tofu-package.py'
        module = ast.parse(path.read_text())
        loops = [node for node in ast.walk(module) if isinstance(node, ast.For)
                 and ast.unparse(node.iter) == "sorted(assets.rglob('*'))"]
        self.assertEqual(len(loops), 1)
        loop = compile(ast.Module(body=loops, type_ignores=[]), str(path), 'exec')
        with tempfile.TemporaryDirectory(prefix='photocraft-discovery-') as temporary:
            assets = Path(temporary) / 'dist'
            expected = {}
            for target, source in self.copied_files().items():
                destination = assets / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                expected['public/' + target] = source.read_bytes()
            (assets / 'private.env').write_text('not a public asset')
            (assets / 'unrelated.txt').write_text('not a public asset')
            output = Path(temporary) / 'package.zip'
            with zipfile.ZipFile(output, 'w') as archive:
                exec(loop, {'assets': assets, 'archive': archive})
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), set(expected))
                for name, data in expected.items():
                    self.assertEqual(archive.read(name), data)


if __name__ == '__main__':
    unittest.main()
