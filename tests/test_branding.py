"""P2 (10 Nov, Hermes-Ride): the app's name on the phone and the reset bar's words (asked for: "RESET – NEW SESSION")."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding='utf-8') as f:
        return f.read()


class Branding(unittest.TestCase):
    def test_page_title_is_the_home_screen_name(self):
        html = read('static', 'index.html')
        self.assertEqual(re.search(r'<title>(.*?)</title>', html).group(1), 'Hermes-Ride')
        self.assertIn('<meta name="apple-mobile-web-app-title" content="Hermes-Ride">', html)

    def test_media_session_names_the_app(self):
        html = read('static', 'index.html')
        self.assertIn("title: 'Hermes-Ride · ' + d, artist: 'Hermes-Ride'", html)
        self.assertNotIn("'Hermes Ride'", html)

    def test_reset_bar_says_reset_new_session(self):
        html = read('static', 'index.html')
        self.assertIn('<span id="resettext">RESET – NEW SESSION</span>', html)
        self.assertIn('aria-label="hold 3 seconds: reset and start a new session"', html)
        self.assertNotIn('hold 3 s · reset', html)

    def test_plugin_and_readme_name(self):
        self.assertIn('label: Hermes-Ride (motorcycle voice)', read('plugin.yaml'))
        self.assertTrue(read('README.md').startswith('# Hermes-Ride\n'))


if __name__ == '__main__':
    unittest.main()
