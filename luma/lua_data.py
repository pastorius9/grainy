"""Bounded parser for Lua *data* tables saved by Lightroom; never executes Lua.

Accepts a table, `return { ... }`, or `s = { ... }`. Calls, expressions,
references, metatables and multiple statements are deliberately not a grammar.
Mixed tables keep their integer array keys; consumers must validate structure.
"""
import math
import re

NUMBER = re.compile(r'[+-]?(?:0[xX][0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?(?:[pP][+-]?\d+)?|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)')
NAME = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')
LONG = re.compile(r'\[(=*)\[')


class DataError(ValueError):
    pass


class Parser:
    def __init__(self, text, max_bytes=16*1024*1024, max_items=200000, max_depth=64):
        if not isinstance(text, str) or len(text) > max_bytes or len(text.encode('utf-8')) > max_bytes:
            raise DataError('저장된 데이터의 크기 또는 문자 형식을 확인할 수 없습니다.')
        self.text = text.lstrip('\ufeff')
        self.position = 0
        self.items = 0
        self.max_items, self.max_depth = max_items, max_depth

    def fail(self):
        raise DataError(f'지원하지 않거나 손상된 데이터 구문입니다 (위치 {self.position}).')

    def long_string(self):
        match = LONG.match(self.text, self.position)
        if not match:
            self.fail()
        end_token = ']' + match[1] + ']'
        start = match.end()
        end = self.text.find(end_token, start)
        if end < 0:
            self.fail()
        self.position = end + len(end_token)
        value = self.text[start:end].replace('\r\n', '\n').replace('\r', '\n')
        return value[1:] if value.startswith('\n') else value

    def space(self):
        while self.position < len(self.text):
            if self.text[self.position].isspace():
                self.position += 1
            elif self.text.startswith('--', self.position):
                self.position += 2
                if LONG.match(self.text, self.position):
                    self.long_string()
                else:
                    end = self.text.find('\n', self.position)
                    self.position = len(self.text) if end < 0 else end + 1
            else:
                break

    def take(self, token):
        self.space()
        if self.text.startswith(token, self.position):
            self.position += len(token)
            return True
        return False

    def quoted(self):
        quote = self.text[self.position]
        self.position += 1
        output = bytearray()
        escapes = {'a':7, 'b':8, 'f':12, 'n':10, 'r':13, 't':9, 'v':11, '\\':92, '"':34, "'":39}
        while self.position < len(self.text):
            char = self.text[self.position]
            self.position += 1
            if char == quote:
                try:
                    return output.decode('utf-8')
                except UnicodeDecodeError:
                    self.fail()
            if char in '\r\n':
                self.fail()
            if char != '\\':
                output.extend(char.encode('utf-8'))
                continue
            if self.position == len(self.text):
                self.fail()
            char = self.text[self.position]
            self.position += 1
            if char in escapes:
                output.append(escapes[char])
            elif char.isascii() and char.isdigit():
                digits = char
                while len(digits) < 3 and self.position < len(self.text) and self.text[self.position] in '0123456789':
                    digits += self.text[self.position]
                    self.position += 1
                if int(digits) > 255:
                    self.fail()
                output.append(int(digits))
            elif char == 'x':
                digits = self.text[self.position:self.position+2]
                if not re.fullmatch('[a-fA-F0-9]{2}', digits):
                    self.fail()
                output.append(int(digits, 16))
                self.position += 2
            elif char == 'z':
                while self.position < len(self.text) and self.text[self.position].isspace():
                    self.position += 1
            elif char in '\r\n':
                if char == '\r' and self.text[self.position:self.position+1] == '\n':
                    self.position += 1
                output.append(10)
            else:
                self.fail()
        self.fail()

    def value(self, depth=0):
        self.space()
        self.items += 1
        if depth > self.max_depth or self.items > self.max_items:
            raise DataError('데이터의 중첩 또는 항목 수가 지원 범위를 넘었습니다.')
        if self.position == len(self.text):
            self.fail()
        char = self.text[self.position]
        if char == '{':
            return self.table(depth+1)
        if char in ('"', "'"):
            return self.quoted()
        if LONG.match(self.text, self.position):
            return self.long_string()
        match = NUMBER.match(self.text, self.position)
        if match:
            token = match[0]
            self.position = match.end()
            if re.fullmatch(r'[+-]?\d+', token):
                return int(token)
            if re.fullmatch(r'[+-]?0[xX][a-fA-F0-9]+', token):
                return int(token,16)
            if 'x' in token.casefold():
                value = float.fromhex(token)
            else:
                value = float(token)
            if not math.isfinite(value):
                self.fail()
            return int(value) if value.is_integer() else value
        match = NAME.match(self.text, self.position)
        if match and match[0] in ('true', 'false', 'nil'):
            self.position = match.end()
            return {'true':True, 'false':False, 'nil':None}[match[0]]
        self.fail()

    def table(self, depth):
        self.position += 1
        result = {}
        auto_key = 1
        if self.take('}'):
            return result
        while True:
            self.space()
            start = self.position
            name = NAME.match(self.text, start)
            if name:
                self.position = name.end()
            if name and self.take('='):
                key = name[0]
            elif self.text.startswith('[', start) and not LONG.match(self.text, start):
                self.position = start + 1
                key = self.value(depth)
                if isinstance(key, bool) or not isinstance(key, (int, float, str)):
                    self.fail()
                if not self.take(']') or not self.take('='):
                    self.fail()
            else:
                self.position = start
                key = auto_key
                auto_key += 1
            if key in result:
                raise DataError('같은 키가 중복된 데이터는 변환하지 않습니다.')
            result[key] = self.value(depth)
            if self.take('}'):
                return result
            if not (self.take(',') or self.take(';')):
                self.fail()
            if self.take('}'):
                return result

    def parse(self):
        self.space()
        name = NAME.match(self.text, self.position)
        if name:
            self.position = name.end()
            if name[0] != 'return' and (name[0] != 's' or not self.take('=')):
                self.fail()
        result = self.value()
        self.take(';')
        self.space()
        if self.position != len(self.text) or not isinstance(result, dict):
            self.fail()
        return result


def parse(text, **limits):
    return Parser(text, **limits).parse()


def sequence(table):
    if not isinstance(table, dict) or any(type(key) is not int or key < 1 for key in table):
        raise DataError('순서가 있는 데이터 배열이 아닙니다.')
    if set(table) != set(range(1, len(table)+1)):
        raise DataError('데이터 배열의 순서가 연속되지 않습니다.')
    return [table[i] for i in range(1, len(table)+1)]
