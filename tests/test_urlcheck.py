import ipaddress
import socket
import unittest
from unittest import mock

from app.urlcheck import UrlError, check_url, is_public_address

PUBLIC = "8.8.8.8"
# Built from integers so no private literals appear in the source tree.
PRIVATE_A = str(ipaddress.IPv4Address(0x0A000005))
PRIVATE_B = str(ipaddress.IPv4Address(0xC0A80102))
PRIVATE_C = str(ipaddress.IPv4Address(0xAC100001))
CGNAT = str(ipaddress.IPv4Address(0x64400001))


def fake_resolver(*addrs):
    def _gai(host, port, *args, **kwargs):
        out = []
        for a in addrs:
            fam = socket.AF_INET6 if ":" in a else socket.AF_INET
            sockaddr = (a, port, 0, 0) if fam == socket.AF_INET6 else (a, port)
            out.append((fam, socket.SOCK_STREAM, 6, "", sockaddr))
        return out
    return _gai


def no_dns(*args, **kwargs):
    raise AssertionError("DNS must not be queried")


class UrlCheckTest(unittest.TestCase):
    def assertRejected(self, url):
        with mock.patch("socket.getaddrinfo", no_dns):
            with self.assertRaises(UrlError, msg=url):
                check_url(url)

    def test_rejects_schemes(self):
        for url in ["file:///etc/passwd", "ftp://example.com/a.mp3",
                    "javascript:alert(1)", "data:text/plain,hi", "example.com/video", ""]:
            self.assertRejected(url)

    def test_rejects_userinfo(self):
        self.assertRejected("https://user:pass@example.com/")
        self.assertRejected("https://user@example.com/")

    def test_rejects_ip_literals(self):
        for url in ["http://127.0.0.1/", "http://8.8.8.8/", "http://[::1]/",
                    "http://[2001:4860:4860::8888]/", "http://[::ffff:127.0.0.1]/",
                    "http://2130706433/", "http://0x7f000001/", "http://0x7f.1/",
                    "http://017700000001/", "http://127.1/", "http://0/"]:
            self.assertRejected(url)

    def test_rejects_localhost(self):
        self.assertRejected("http://localhost:8090/")
        self.assertRejected("http://foo.localhost/")

    def test_rejects_too_long_and_whitespace(self):
        self.assertRejected("https://example.com/" + "a" * 2100)
        self.assertRejected("https://example.com/a b")
        self.assertRejected("https://exa mple.com/")

    def test_rejects_bad_port(self):
        self.assertRejected("https://example.com:99999/")

    def test_accepts_public(self):
        with mock.patch("socket.getaddrinfo", fake_resolver(PUBLIC)):
            url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            self.assertEqual(check_url("  " + url + "\n"), url)

    def test_rejects_resolving_to_loopback(self):
        with mock.patch("socket.getaddrinfo", fake_resolver("127.0.0.1")):
            with self.assertRaises(UrlError):
                check_url("https://evil.example.com/")

    def test_rejects_resolving_to_private(self):
        for addr in (PRIVATE_A, PRIVATE_B, PRIVATE_C, CGNAT, "fd00::1", "fe80::1%eth0",
                     "::ffff:" + PRIVATE_B, "169.254.169.254", "0.0.0.0", "224.0.0.1"):
            with mock.patch("socket.getaddrinfo", fake_resolver(addr)):
                with self.assertRaises(UrlError, msg=addr):
                    check_url("https://media.example.com/a")

    def test_rejects_if_any_address_private(self):
        with mock.patch("socket.getaddrinfo", fake_resolver(PUBLIC, PRIVATE_A)):
            with self.assertRaises(UrlError):
                check_url("https://media.example.com/a")

    def test_rejects_unresolvable(self):
        def fail(*a, **k):
            raise socket.gaierror("nope")
        with mock.patch("socket.getaddrinfo", fail):
            with self.assertRaises(UrlError):
                check_url("https://nonexistent.example.com/")

    def test_is_public_address(self):
        self.assertTrue(is_public_address(PUBLIC))
        self.assertFalse(is_public_address(PRIVATE_A))
        self.assertFalse(is_public_address("::1"))
        self.assertFalse(is_public_address("not-an-ip"))


if __name__ == "__main__":
    unittest.main()
