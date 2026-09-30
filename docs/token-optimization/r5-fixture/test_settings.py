"""冻结的消费者验收；初始模块故意有失败，不在仓库默认测试集内。"""
import unittest
from settings import parse_retry_settings


class Contract(unittest.TestCase):
    def assert_invalid_line(self, text, line):
        with self.assertRaises(ValueError) as error:
            parse_retry_settings(text)
        self.assertIn(f'line {line}', str(error.exception))

    def test_defaults_and_normal_input(self):
        self.assertEqual(parse_retry_settings(''), {'retries': 3, 'backoff_ms': 100})
        self.assertEqual(parse_retry_settings('retries=2\nbackoff_ms=50'), {'retries': 2, 'backoff_ms': 50})

    def test_repeated_key(self):
        self.assert_invalid_line('retries=2\nretries=1', 2)

    def test_zero(self):
        self.assertEqual(parse_retry_settings('retries=0\nbackoff_ms=0'), {'retries': 0, 'backoff_ms': 0})

    def test_negative(self):
        self.assert_invalid_line('retries=-1', 1)

    def test_empty_value(self):
        self.assert_invalid_line('retries=1\nbackoff_ms=', 2)

    def test_blank_and_whitespace(self):
        text = '\n  retries = 2  \n \nbackoff_ms=3\n'
        self.assertEqual(parse_retry_settings(text), {'retries': 2, 'backoff_ms': 3})
        self.assertEqual(text, '\n  retries = 2  \n \nbackoff_ms=3\n')

    def test_unknown_key(self):
        self.assert_invalid_line('other=2', 1)

    def test_invalid_number(self):
        self.assert_invalid_line('retries=1\nbackoff_ms=oops', 2)

    def test_malformed_line(self):
        self.assert_invalid_line('retries=1=2', 1)


if __name__ == '__main__':
    unittest.main()
