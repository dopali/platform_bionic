#!/usr/bin/env python
"""A glorified C pre-processor parser."""

import ctypes
import logging
import os
import re
import site
import unittest
import utils

top = os.getenv('ANDROID_BUILD_TOP')
if top is None:
    utils.panic('ANDROID_BUILD_TOP not set.\n')

# Set up the env vars for libclang.
site.addsitedir(os.path.join(top, 'external/clang/bindings/python'))

import clang.cindex
from clang.cindex import conf
from clang.cindex import Cursor
from clang.cindex import CursorKind
from clang.cindex import SourceLocation
from clang.cindex import SourceRange
from clang.cindex import TokenGroup
from clang.cindex import TokenKind
from clang.cindex import TranslationUnit

# Set up LD_LIBRARY_PATH to include libclang.so, libLLVM.so, and etc.
# Note that setting LD_LIBRARY_PATH with os.putenv() sometimes doesn't help.
clang.cindex.Config.set_library_file(os.path.join(top, 'prebuilts/sdk/tools/linux/lib64/libclang_android.so'))

from defaults import kCppUndefinedMacro
from defaults import kernel_remove_config_macros
from defaults import kernel_struct_replacements
from defaults import kernel_token_replacements


debugBlockParser = False
debugCppExpr = False
debugOptimIf01 = False

###############################################################################
###############################################################################
#####                                                                     #####
#####           C P P   T O K E N S                                       #####
#####                                                                     #####
###############################################################################
###############################################################################

# the list of supported C-preprocessor tokens
# plus a couple of C tokens as well
tokEOF = "\0"
tokLN = "\n"
tokSTRINGIFY = "#"
tokCONCAT = "##"
tokLOGICAND = "&&"
tokLOGICOR = "||"
tokSHL = "<<"
tokSHR = ">>"
tokEQUAL = "=="
tokNEQUAL = "!="
tokLT = "<"
tokLTE = "<="
tokGT = ">"
tokGTE = ">="
tokELLIPSIS = "..."
tokSPACE = " "
tokDEFINED = "defined"
tokLPAREN = "("
tokRPAREN = ")"
tokNOT = "!"
tokPLUS = "+"
tokMINUS = "-"
tokMULTIPLY = "*"
tokDIVIDE = "/"
tokMODULUS = "%"
tokBINAND = "&"
tokBINOR = "|"
tokBINXOR = "^"
tokCOMMA = ","
tokLBRACE = "{"
tokRBRACE = "}"
tokARROW = "->"
tokINCREMENT = "++"
tokDECREMENT = "--"
tokNUMBER = "<number>"
tokIDENT = "<ident>"
tokSTRING = "<string>"


class Token(clang.cindex.Token):
    """A class that represents one token after parsing.

    It inherits the class in libclang, with an extra id property to hold the
    new spelling of the token. The spelling property in the base class is
    defined as read-only. New names after macro instantiation are saved in
    their ids now. It also facilitates the renaming of directive optimizations
    like replacing 'ifndef X' with 'if !defined(X)'.

    It also overrides the cursor property of the base class. Because the one
    in libclang always queries based on a single token, which usually doesn't
    hold useful information. The cursor in this class can be set by calling
    CppTokenizer.getTokensWithCursors(). Otherwise it returns the one in the
    base class.
    """

    def __init__(self, tu=None, group=None, int_data=None, ptr_data=None,
                 cursor=None):
        clang.cindex.Token.__init__(self)
        self._id = None
        self._tu = tu
        self._group = group
        self._cursor = cursor
        # self.int_data and self.ptr_data are from the base class. But
        # self.int_data doesn't accept a None value.
        if int_data is not None:
            self.int_data = int_data
        self.ptr_data = ptr_data

    @property
    def id(self):
        """Name of the token."""
        if self._id is None:
            return self.spelling
        else:
            return self._id

    @id.setter
    def id(self, new_id):
        """Setting name of the token."""
        self._id = new_id

    @property
    def cursor(self):
        if self._cursor is None:
            self._cursor = clang.cindex.Token.cursor
        return self._cursor

    @cursor.setter
    def cursor(self, new_cursor):
        self._cursor = new_cursor

    def __repr__(self):
        if self.id == 'defined':
            return self.id
        elif self.kind == TokenKind.IDENTIFIER:
            return "(ident %s)" % self.id

        return self.id

    def __str__(self):
        return self.id


class BadExpectedToken(Exception):
    """An exception that will be raised for unexpected tokens."""
    pass


# The __contains__ function in libclang SourceRange class contains a bug. It
# gives wrong result when dealing with single line range.
# Bug filed with upstream:
# http://llvm.org/bugs/show_bug.cgi?id=22243, http://reviews.llvm.org/D7277
def SourceRange__contains__(self, other):
    """Determine if a given location is inside the range."""
    if not isinstance(other, SourceLocation):
        return False
    if other.file is None and self.start.file is None:
        pass
    elif (self.start.file.name != other.file.name or
          other.file.name != self.end.file.name):
        # same file name
        return False
    # same file, in between lines
    if self.start.line < other.line < self.end.line:
        return True
    # same file, same line
    elif self.start.line == other.line == self.end.line:
        if self.start.column <= other.column <= self.end.column:
            return True
    elif self.start.line == other.line:
        # same file first line
        if self.start.column <= other.column:
            return True
    elif other.line == self.end.line:
        # same file last line
        if other.column <= self.end.column:
            return True
    return False


SourceRange.__contains__ = SourceRange__contains__


################################################################################
################################################################################
#####                                                                      #####
#####           C P P   T O K E N I Z E R                                  #####
#####                                                                      #####
################################################################################
################################################################################


class CppTokenizer(object):
    """A tokenizer that converts some input text into a list of tokens.

    It calls libclang's tokenizer to get the parsed tokens. In addition, it
    updates the cursor property in each token after parsing, by calling
    getTokensWithCursors().
    """

    clang_flags = ['-E', '-x', 'c']
    options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD

    def __init__(self):
        """Initialize a new CppTokenizer object."""
        self._indexer = clang.cindex.Index.create()
        self._tu = None
        self._index = 0
        self.tokens = None

    def _getTokensWithCursors(self):
        """Helper method to return all tokens with their cursors.

        The cursor property in a clang Token doesn't provide enough
        information. Because it is queried based on single token each time
        without any context, i.e. via calling conf.lib.clang_annotateTokens()
        with only one token given. So we often see 'INVALID_FILE' in one
        token's cursor. In this function it passes all the available tokens
        to get more informative cursors.
        """

        tokens_memory = ctypes.POINTER(clang.cindex.Token)()
        tokens_count = ctypes.c_uint()

        conf.lib.clang_tokenize(self._tu, self._tu.cursor.extent,
                                ctypes.byref(tokens_memory),
                                ctypes.byref(tokens_count))

        count = int(tokens_count.value)

        # If we get no tokens, no memory was allocated. Be sure not to return
        # anything and potentially call a destructor on nothing.
        if count < 1:
            return

        cursors = (Cursor * count)()
        cursors_memory = ctypes.cast(cursors, ctypes.POINTER(Cursor))

        conf.lib.clang_annotateTokens(self._tu, tokens_memory, count,
                                      cursors_memory)

        tokens_array = ctypes.cast(
            tokens_memory,
            ctypes.POINTER(clang.cindex.Token * count)).contents
        token_group = TokenGroup(self._tu, tokens_memory, tokens_count)

        tokens = []
        for i in xrange(0, count):
            token = Token(self._tu, token_group,
                          int_data=tokens_array[i].int_data,
                          ptr_data=tokens_array[i].ptr_data,
                          cursor=cursors[i])
            # We only want non-comment tokens.
            if token.kind != TokenKind.COMMENT:
                tokens.append(token)

        return tokens

    def parseString(self, lines):
        """Parse a list of text lines into a BlockList object."""
        file_ = 'dummy.c'
        self._tu = self._indexer.parse(file_, self.clang_flags,
                                       unsaved_files=[(file_, lines)],
                                       options=self.options)
        self.tokens = self._getTokensWithCursors()

    def parseFile(self, file_):
        """Parse a file into a BlockList object."""
        self._tu = self._indexer.parse(file_, self.clang_flags,
                                       options=self.options)
        self.tokens = self._getTokensWithCursors()

    def nextToken(self):
        """Return next token from the list."""
        if self._index < len(self.tokens):
            t = self.tokens[self._index]
            self._index += 1
            return t
        else:
            return None


class CppStringTokenizer(CppTokenizer):
    """A CppTokenizer derived class that accepts a string of text as input."""

    def __init__(self, line):
        CppTokenizer.__init__(self)
        self.parseString(line)


class CppFileTokenizer(CppTokenizer):
    """A CppTokenizer derived class that accepts a file as input."""

    def __init__(self, file_):
        CppTokenizer.__init__(self)
        self.parseFile(file_)


# Unit testing
#
class CppTokenizerTests(unittest.TestCase):
    """CppTokenizer tests."""

    def get_tokens(self, token_string, line_col=False):
        tokens = CppStringTokenizer(token_string)
        token_list = []
        while True:
            token = tokens.nextToken()
            if not token:
                break
            if line_col:
                token_list.append((token.id, token.location.line,
                                   token.location.column))
            else:
                token_list.append(token.id)
        return token_list

    def test_hash(self):
        self.assertEqual(self.get_tokens("#an/example  && (01923_xy)"),
                         ["#", "an", "/", "example", tokLOGICAND, tokLPAREN,
                          "01923_xy", tokRPAREN])

    def test_parens(self):
        self.assertEqual(self.get_tokens("FOO(BAR) && defined(BAZ)"),
                         ["FOO", tokLPAREN, "BAR", tokRPAREN, tokLOGICAND,
                          "defined", tokLPAREN, "BAZ", tokRPAREN])

    def test_comment(self):
        self.assertEqual(self.get_tokens("/*\n#\n*/"), [])

    def test_line_cross(self):
        self.assertEqual(self.get_tokens("first\nsecond"), ["first", "second"])

    def test_line_cross_line_col(self):
        self.assertEqual(self.get_tokens("first second\n  third", True),
                         [("first", 1, 1), ("second", 1, 7), ("third", 2, 3)])

    def test_comment_line_col(self):
        self.assertEqual(self.get_tokens("boo /* what the\nhell */", True),
                         [("boo", 1, 1)])

    def test_escapes(self):
        self.assertEqual(self.get_tokens("an \\\n example", True),
                         [("an", 1, 1), ("example", 2, 2)])


################################################################################
################################################################################
#####                                                                      #####
#####           C P P   E X P R E S S I O N S                              #####
#####                                                                      #####
################################################################################
################################################################################


class CppExpr(object):
    """A class that models the condition of #if directives into an expr tree.

    Each node in the tree is of the form (op, arg) or (op, arg1, arg2) where
    "op" is a string describing the operation
    """

    unaries = ["!", "~"]
    binaries = ["+", "-", "<", "<=", ">=", ">", "&&", "||", "*", "/", "%",
                "&", "|", "^", "<<", ">>", "==", "!=", "?", ":"]
    precedences = {
        "?": 1, ":": 1,
        "||": 2,
        "&&": 3,
        "|": 4,
        "^": 5,
        "&": 6,
        "==": 7, "!=": 7,
        "<": 8, "<=": 8, ">": 8, ">=": 8,
        "<<": 9, ">>": 9,
        "+": 10, "-": 10,
        "*": 11, "/": 11, "%": 11,
        "!": 12, "~": 12
    }

    def __init__(self, tokens):
        """Initialize a CppExpr. 'tokens' must be a CppToken list."""
        self.tokens = tokens
        self._num_tokens = len(tokens)
        self._index = 0

        if debugCppExpr:
            print "CppExpr: trying to parse %s" % repr(tokens)
        self.expr = self.parseExpression(0)
        if debugCppExpr:
            print "CppExpr: got " + repr(self.expr)
        if self._index != self._num_tokens:
            self.throw(BadExpectedToken, "crap at end of input (%d != %d): %s"
                       % (self._index, self._num_tokens, repr(tokens)))

    def throw(self, exception, msg):
        if self._index < self._num_tokens:
            tok = self.tokens[self._index]
            print "%d:%d: %s" % (tok.location.line, tok.location.column, msg)
        else:
            print "EOF: %s" % msg
        raise exception(msg)

    def expectId(self, id):
        """Check that a given token id is at the current position."""
        token = self.tokens[self._index]
        if self._index >= self._num_tokens or token.id != id:
            self.throw(BadExpectedToken,
                       "### expecting '%s' in expression, got '%s'" % (
                           id, token.id))
        self._index += 1

    def is_decimal(self):
        token = self.tokens[self._index].id
        if token[-1] in "ULul":
            token = token[:-1]
        try:
            val = int(token, 10)
            self._index += 1
            return ('int', val)
        except ValueError:
            return None

    def is_octal(self):
        token = self.tokens[self._index].id
        if token[-1] in "ULul":
            token = token[:-1]
        if len(token) < 2 or token[0] != '0':
            return None
        try:
            val = int(token, 8)
            self._index += 1
            return ('oct', val)
        except ValueError:
            return None

    def is_hexadecimal(self):
        token = self.tokens[self._index].id
        if token[-1] in "ULul":
            token = token[:-1]
        if len(token) < 3 or (token[:2] != '0x' and token[:2] != '0X'):
            return None
        try:
            val = int(token, 16)
            self._index += 1
            return ('hex', val)
        except ValueError:
            return None

    def is_integer(self):
        if self.tokens[self._index].kind != TokenKind.LITERAL:
            return None

        c = self.is_hexadecimal()
        if c:
            return c

        c = self.is_octal()
        if c:
            return c

        c = self.is_decimal()
        if c:
            return c

        return None

    def is_number(self):
        t = self.tokens[self._index]
        if t.id == tokMINUS and self._index + 1 < self._num_tokens:
            self._index += 1
            c = self.is_integer()
            if c:
                op, val = c
                return (op, -val)
        if t.id == tokPLUS and self._index + 1 < self._num_tokens:
            self._index += 1
            c = self.is_integer()
            if c:
                return c

        return self.is_integer()

    def is_defined(self):
        t = self.tokens[self._index]
        if t.id != tokDEFINED:
            return None

        # We have the defined keyword, check the rest.
        self._index += 1
        used_parens = False
        if (self._index < self._num_tokens and
            self.tokens[self._index].id == tokLPAREN):
            used_parens = True
            self._index += 1

        if self._index >= self._num_tokens:
            self.throw(BadExpectedToken,
                       "### 'defined' must be followed by macro name or left "
                       "paren")

        t = self.tokens[self._index]
        if t.kind != TokenKind.IDENTIFIER:
            self.throw(BadExpectedToken,
                       "### 'defined' must be followed by macro name")

        self._index += 1
        if used_parens:
            self.expectId(tokRPAREN)

        return ("defined", t.id)

    def is_call_or_ident(self):
        if self._index >= self._num_tokens:
            return None

        t = self.tokens[self._index]
        if t.kind != TokenKind.IDENTIFIER:
            return None

        name = t.id

        self._index += 1
        if (self._index >= self._num_tokens or
            self.tokens[self._index].id != tokLPAREN):
            return ("ident", name)

        params = []
        depth = 1
        self._index += 1
        j = self._index
        while self._index < self._num_tokens:
            id = self.tokens[self._index].id
            if id == tokLPAREN:
                depth += 1
            elif depth == 1 and (id == tokCOMMA or id == tokRPAREN):
                k = self._index
                param = self.tokens[j:k]
                params.append(param)
                if id == tokRPAREN:
                    break
                j = self._index + 1
            elif id == tokRPAREN:
                depth -= 1
            self._index += 1

        if self._index >= self._num_tokens:
            return None

        self._index += 1
        return ("call", (name, params))

    # Implements the "precedence climbing" algorithm from
    # http://www.engr.mun.ca/~theo/Misc/exp_parsing.htm.
    # The "classic" algorithm would be fine if we were using a tool to
    # generate the parser, but we're not. Dijkstra's "shunting yard"
    # algorithm hasn't been necessary yet.

    def parseExpression(self, minPrecedence):
        if self._index >= self._num_tokens:
            return None

        node = self.parsePrimary()
        while (self.token() and self.isBinary(self.token()) and
               self.precedence(self.token()) >= minPrecedence):
            op = self.token()
            self.nextToken()
            rhs = self.parseExpression(self.precedence(op) + 1)
            node = (op.id, node, rhs)

        return node

    def parsePrimary(self):
        op = self.token()
        if self.isUnary(op):
            self.nextToken()
            return (op.id, self.parseExpression(self.precedence(op)))

        primary = None
        if op.id == tokLPAREN:
            self.nextToken()
            primary = self.parseExpression(0)
            self.expectId(tokRPAREN)
        elif op.id == "?":
            self.nextToken()
            primary = self.parseExpression(0)
            self.expectId(":")
        elif op.id == '+' or op.id == '-' or op.kind == TokenKind.LITERAL:
            primary = self.is_number()
        # Checking for 'defined' needs to come first now because 'defined' is
        # recognized as IDENTIFIER.
        elif op.id == tokDEFINED:
            primary = self.is_defined()
        elif op.kind == TokenKind.IDENTIFIER:
            primary = self.is_call_or_ident()
        else:
            self.throw(BadExpectedToken,
                       "didn't expect to see a %s in factor" % (
                           self.tokens[self._index].id))
        return primary

    def isBinary(self, token):
        return token.id in self.binaries

    def isUnary(self, token):
        return token.id in self.unaries

    def precedence(self, token):
        return self.precedences.get(token.id)

    def token(self):
        if self._index >= self._num_tokens:
            return None
        return self.tokens[self._index]

    def nextToken(self):
        self._index += 1
        if self._index >= self._num_tokens:
            return None
        return self.tokens[self._index]

    def dump_node(self, e):
        op = e[0]
        line = "(" + op
        if op == "int":
            line += " %d)" % e[1]
        elif op == "oct":
            line += " 0%o)" % e[1]
        elif op == "hex":
            line += " 0x%x)" % e[1]
        elif op == "ident":
            line += " %s)" % e[1]
        elif op == "defined":
            line += " %s)" % e[1]
        elif op == "call":
            arg = e[1]
            line += " %s [" % arg[0]
            prefix = ""
            for param in arg[1]:
                par = ""
                for tok in param:
                    par += str(tok)
                line += "%s%s" % (prefix, par)
                prefix = ","
            line += "])"
        elif op in CppExpr.unaries:
            line += " %s)" % self.dump_node(e[1])
        elif op in CppExpr.binaries:
            line += " %s %s)" % (self.dump_node(e[1]), self.dump_node(e[2]))
        else:
            line += " ?%s)" % repr(e[1])

        return line

    def __repr__(self):
        return self.dump_node(self.expr)

    def source_node(self, e):
        op = e[0]
        if op == "int":
            return "%d" % e[1]
        if op == "hex":
            return "0x%x" % e[1]
        if op == "oct":
            return "0%o" % e[1]
        if op == "ident":
            # XXX: should try to expand
            return e[1]
        if op == "defined":
            return "defined(%s)" % e[1]

        prec = CppExpr.precedences.get(op, 1000)
        arg = e[1]
        if op in CppExpr.unaries:
            arg_src = self.source_node(arg)
            arg_op = arg[0]
            arg_prec = CppExpr.precedences.get(arg_op, 1000)
            if arg_prec < prec:
                return "!(" + arg_src + ")"
            else:
                return "!" + arg_src
        if op in CppExpr.binaries:
            arg2 = e[2]
            arg1_op = arg[0]
            arg2_op = arg2[0]
            arg1_src = self.source_node(arg)
            arg2_src = self.source_node(arg2)
            if CppExpr.precedences.get(arg1_op, 1000) < prec:
                arg1_src = "(%s)" % arg1_src
            if CppExpr.precedences.get(arg2_op, 1000) < prec:
                arg2_src = "(%s)" % arg2_src

            return "%s %s %s" % (arg1_src, op, arg2_src)
        return "???"

    def __str__(self):
        return self.source_node(self.expr)

    @staticmethod
    def int_node(e):
        if e[0] in ["int", "oct", "hex"]:
            return e[1]
        else:
            return None

    def toInt(self):
        return self.int_node(self.expr)

    def optimize_node(self, e, macros=None):
        if macros is None:
            macros = {}
        op = e[0]

        if op == "defined":
            op, name = e
            if macros.has_key(name):
                if macros[name] == kCppUndefinedMacro:
                    return ("int", 0)
                else:
                    try:
                        value = int(macros[name])
                        return ("int", value)
                    except ValueError:
                        return ("defined", macros[name])

            if kernel_remove_config_macros and name.startswith("CONFIG_"):
                return ("int", 0)

            return e

        elif op == "ident":
            op, name = e
            if macros.has_key(name):
                try:
                    value = int(macros[name])
                    expanded = ("int", value)
                except ValueError:
                    expanded = ("ident", macros[name])
                return self.optimize_node(expanded, macros)
            return e

        elif op == "!":
            op, v = e
            v = self.optimize_node(v, macros)
            if v[0] == "int":
                if v[1] == 0:
                    return ("int", 1)
                else:
                    return ("int", 0)
            return ('!', v)

        elif op == "&&":
            op, l, r = e
            l = self.optimize_node(l, macros)
            r = self.optimize_node(r, macros)
            li = self.int_node(l)
            ri = self.int_node(r)
            if li is not None:
                if li == 0:
                    return ("int", 0)
                else:
                    return r
            elif ri is not None:
                if ri == 0:
                    return ("int", 0)
                else:
                    return l
            return (op, l, r)

        elif op == "||":
            op, l, r = e
            l = self.optimize_node(l, macros)
            r = self.optimize_node(r, macros)
            li = self.int_node(l)
            ri = self.int_node(r)
            if li is not None:
                if li == 0:
                    return r
                else:
                    return ("int", 1)
            elif ri is not None:
                if ri == 0:
                    return l
                else:
                    return ("int", 1)
            return (op, l, r)

        else:
            return e

    def optimize(self, macros=None):
        if macros is None:
            macros = {}
        self.expr = self.optimize_node(self.expr, macros)

class CppExprTest(unittest.TestCase):
    """CppExpr unit tests."""

    def get_expr(self, expr):
        return repr(CppExpr(CppStringTokenizer(expr).tokens))

    def test_cpp_expr(self):
        self.assertEqual(self.get_expr("0"), "(int 0)")
        self.assertEqual(self.get_expr("1"), "(int 1)")
        self.assertEqual(self.get_expr("-5"), "(int -5)")
        self.assertEqual(self.get_expr("+1"), "(int 1)")
        self.assertEqual(self.get_expr("0U"), "(int 0)")
        self.assertEqual(self.get_expr("015"), "(oct 015)")
        self.assertEqual(self.get_expr("015l"), "(oct 015)")
        self.assertEqual(self.get_expr("0x3e"), "(hex 0x3e)")
        self.assertEqual(self.get_expr("(0)"), "(int 0)")
        self.assertEqual(self.get_expr("1 && 1"), "(&& (int 1) (int 1))")
        self.assertEqual(self.get_expr("1 && 0"), "(&& (int 1) (int 0))")
        self.assertEqual(self.get_expr("EXAMPLE"), "(ident EXAMPLE)")
        self.assertEqual(self.get_expr("EXAMPLE - 3"),
                         "(- (ident EXAMPLE) (int 3))")
        self.assertEqual(self.get_expr("defined(EXAMPLE)"),
                         "(defined EXAMPLE)")
        self.assertEqual(self.get_expr("defined ( EXAMPLE ) "),
                         "(defined EXAMPLE)")
        self.assertEqual(self.get_expr("!defined(EXAMPLE)"),
                         "(! (defined EXAMPLE))")
        self.assertEqual(self.get_expr("defined(ABC) || defined(BINGO)"),
                         "(|| (defined ABC) (defined BINGO))")
        self.assertEqual(self.get_expr("FOO(BAR,5)"), "(call FOO [BAR,5])")
        self.assertEqual(self.get_expr("A == 1 || defined(B)"),
                         "(|| (== (ident A) (int 1)) (defined B))")

    def get_expr_optimize(self, expr, macros=None):
        if macros is None:
            macros = {}
        e = CppExpr(CppStringTokenizer(expr).tokens)
        e.optimize(macros)
        return repr(e)

    def test_cpp_expr_optimize(self):
        self.assertEqual(self.get_expr_optimize("0"), "(int 0)")
        self.assertEqual(self.get_expr_optimize("1"), "(int 1)")
        self.assertEqual(self.get_expr_optimize("1 && 1"), "(int 1)")
        self.assertEqual(self.get_expr_optimize("1 && +1"), "(int 1)")
        self.assertEqual(self.get_expr_optimize("0x1 && 01"), "(oct 01)")
        self.assertEqual(self.get_expr_optimize("1 && 0"), "(int 0)")
        self.assertEqual(self.get_expr_optimize("0 && 1"), "(int 0)")
        self.assertEqual(self.get_expr_optimize("0 && 0"), "(int 0)")
        self.assertEqual(self.get_expr_optimize("1 || 1"), "(int 1)")
        self.assertEqual(self.get_expr_optimize("1 || 0"), "(int 1)")
        self.assertEqual(self.get_expr_optimize("0 || 1"), "(int 1)")
        self.assertEqual(self.get_expr_optimize("0 || 0"), "(int 0)")
        self.assertEqual(self.get_expr_optimize("A"), "(ident A)")
        self.assertEqual(self.get_expr_optimize("A", {"A": 1}), "(int 1)")
        self.assertEqual(self.get_expr_optimize("A || B", {"A": 1}), "(int 1)")
        self.assertEqual(self.get_expr_optimize("A || B", {"B": 1}), "(int 1)")
        self.assertEqual(self.get_expr_optimize("A && B", {"A": 1}), "(ident B)")
        self.assertEqual(self.get_expr_optimize("A && B", {"B": 1}), "(ident A)")
        self.assertEqual(self.get_expr_optimize("A && B"), "(&& (ident A) (ident B))")
        self.assertEqual(self.get_expr_optimize("EXAMPLE"), "(ident EXAMPLE)")
        self.assertEqual(self.get_expr_optimize("EXAMPLE - 3"), "(- (ident EXAMPLE) (int 3))")
        self.assertEqual(self.get_expr_optimize("defined(EXAMPLE)"), "(defined EXAMPLE)")
        self.assertEqual(self.get_expr_optimize("defined(EXAMPLE)",
                                                {"EXAMPLE": "XOWOE"}),
                         "(defined XOWOE)")
        self.assertEqual(self.get_expr_optimize("defined(EXAMPLE)",
                                                {"EXAMPLE": kCppUndefinedMacro}),
                         "(int 0)")
        self.assertEqual(self.get_expr_optimize("!defined(EXAMPLE)"), "(! (defined EXAMPLE))")
        self.assertEqual(self.get_expr_optimize("!defined(EXAMPLE)",
                                                {"EXAMPLE": "XOWOE"}),
                         "(! (defined XOWOE))")
        self.assertEqual(self.get_expr_optimize("!defined(EXAMPLE)",
                                                {"EXAMPLE": kCppUndefinedMacro}),
                         "(int 1)")
        self.assertEqual(self.get_expr_optimize("defined(A) || defined(B)"),
                        "(|| (defined A) (defined B))")
        self.assertEqual(self.get_expr_optimize("defined(A) || defined(B)",
                                                {"A": "1"}),
                         "(int 1)")
        self.assertEqual(self.get_expr_optimize("defined(A) || defined(B)",
                                                {"B": "1"}),
                         "(int 1)")
        self.assertEqual(self.get_expr_optimize("defined(A) || defined(B)",
                                                {"B": kCppUndefinedMacro}),
                         "(defined A)")
        self.assertEqual(self.get_expr_optimize("defined(A) || defined(B)",
                                                {"A": kCppUndefinedMacro,
                                                 "B": kCppUndefinedMacro}),
                         "(int 0)")
        self.assertEqual(self.get_expr_optimize("defined(A) && defined(B)"),
                         "(&& (defined A) (defined B))")
        self.assertEqual(self.get_expr_optimize("defined(A) && defined(B)",
                                                {"A": "1"}),
                         "(defined B)")
        self.assertEqual(self.get_expr_optimize("defined(A) && defined(B)",
                                                {"B": "1"}),
                         "(defined A)")
        self.assertEqual(self.get_expr_optimize("defined(A) && defined(B)",
                                                {"B": kCppUndefinedMacro}),
                        "(int 0)")
        self.assertEqual(self.get_expr_optimize("defined(A) && defined(B)",
                                                {"A": kCppUndefinedMacro}),
                        "(int 0)")
        self.assertEqual(self.get_expr_optimize("A == 1 || defined(B)"),
                         "(|| (== (ident A) (int 1)) (defined B))")
        self.assertEqual(self.get_expr_optimize(
              "defined(__KERNEL__) || !defined(__GLIBC__) || (__GLIBC__ < 2)",
              {"__KERNEL__": kCppUndefinedMacro}),
              "(|| (! (defined __GLIBC__)) (< (ident __GLIBC__) (int 2)))")

    def get_expr_string(self, expr):
        return str(CppExpr(CppStringTokenizer(expr).tokens))

    def test_cpp_expr_string(self):
        self.assertEqual(self.get_expr_string("0"), "0")
        self.assertEqual(self.get_expr_string("1"), "1")
        self.assertEqual(self.get_expr_string("1 && 1"), "1 && 1")
        self.assertEqual(self.get_expr_string("1 && 0"), "1 && 0")
        self.assertEqual(self.get_expr_string("0 && 1"), "0 && 1")
        self.assertEqual(self.get_expr_string("0 && 0"), "0 && 0")
        self.assertEqual(self.get_expr_string("1 || 1"), "1 || 1")
        self.assertEqual(self.get_expr_string("1 || 0"), "1 || 0")
        self.assertEqual(self.get_expr_string("0 || 1"), "0 || 1")
        self.assertEqual(self.get_expr_string("0 || 0"), "0 || 0")
        self.assertEqual(self.get_expr_string("EXAMPLE"), "EXAMPLE")
        self.assertEqual(self.get_expr_string("EXAMPLE - 3"), "EXAMPLE - 3")
        self.assertEqual(self.get_expr_string("defined(EXAMPLE)"), "defined(EXAMPLE)")
        self.assertEqual(self.get_expr_string("defined EXAMPLE"), "defined(EXAMPLE)")
        self.assertEqual(self.get_expr_string("A == 1 || defined(B)"), "A == 1 || defined(B)")


################################################################################
################################################################################
#####                                                                      #####
#####          C P P   B L O C K                                           #####
#####                                                                      #####
################################################################################
################################################################################


class Block(object):
    """A class used to model a block of input source text.

    There are two block types:
      - directive blocks: contain the tokens of a single pre-processor
        directive (e.g. #if)
      - text blocks, contain the tokens of non-directive blocks

    The cpp parser class below will transform an input source file into a list
    of Block objects (grouped in a BlockList object for convenience)
    """

    def __init__(self, tokens, directive=None, lineno=0, identifier=None):
        """Initialize a new block, if 'directive' is None, it is a text block.

        NOTE: This automatically converts '#ifdef MACRO' into
        '#if defined(MACRO)' and '#ifndef MACRO' into '#if !defined(MACRO)'.
        """

        if directive == "ifdef":
            tok = Token()
            tok.id = tokDEFINED
            tokens = [tok] + tokens
            directive = "if"

        elif directive == "ifndef":
            tok1 = Token()
            tok2 = Token()
            tok1.id = tokNOT
            tok2.id = tokDEFINED
            tokens = [tok1, tok2] + tokens
            directive = "if"

        self.tokens = tokens
        self.directive = directive
        self.define_id = identifier
        if lineno > 0:
            self.lineno = lineno
        else:
            self.lineno = self.tokens[0].location.line

        if self.isIf():
            self.expr = CppExpr(self.tokens)

    def isDirective(self):
        """Return True iff this is a directive block."""
        return self.directive is not None

    def isConditional(self):
        """Return True iff this is a conditional directive block."""
        return self.directive in ["if", "ifdef", "ifndef", "else", "elif",
                                  "endif"]

    def isDefine(self):
        """Return the macro name in a #define directive, or None otherwise."""
        if self.directive != "define":
            return None
        return self.define_id

    def isIf(self):
        """Return True iff this is an #if-like directive block."""
        return self.directive in ["if", "ifdef", "ifndef", "elif"]

    def isEndif(self):
        """Return True iff this is an #endif directive block."""
        return self.directive == "endif"

    def isInclude(self):
        """Check whether this is a #include directive.

        If true, returns the corresponding file name (with brackets or
        double-qoutes). None otherwise.
        """

        if self.directive != "include":
            return None
        return ''.join([str(x) for x in self.tokens])

    @staticmethod
    def format_blocks(tokens, indent=0):
        """Return the formatted lines of strings with proper indentation."""
        newline = True
        result = []
        buf = ''
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.id == '{':
                buf += ' {'
                result.append(strip_space(buf))
                indent += 2
                buf = ''
                newline = True
            elif t.id == '}':
                indent -= 2
                if not newline:
                    result.append(strip_space(buf))
                # Look ahead to determine if it's the end of line.
                if (i + 1 < len(tokens) and
                    (tokens[i+1].id == ';' or
                     tokens[i+1].id in ['else', '__attribute__',
                                        '__attribute', '__packed'] or
                     tokens[i+1].kind == TokenKind.IDENTIFIER)):
                    buf = ' ' * indent + '}'
                    newline = False
                else:
                    result.append(' ' * indent + '}')
                    buf = ''
                    newline = True
            elif t.id == ';':
                result.append(strip_space(buf) + ';')
                buf = ''
                newline = True
            # We prefer a new line for each constant in enum.
            elif t.id == ',' and t.cursor.kind == CursorKind.ENUM_DECL:
                result.append(strip_space(buf) + ',')
                buf = ''
                newline = True
            else:
                if newline:
                    buf += ' ' * indent + str(t)
                else:
                    buf += ' ' + str(t)
                newline = False
            i += 1

        if buf:
            result.append(strip_space(buf))

        return result, indent

    def write(self, out, indent):
        """Dump the current block."""
        # removeWhiteSpace() will sometimes creates non-directive blocks
        # without any tokens. These come from blocks that only contained
        # empty lines and spaces. They should not be printed in the final
        # output, and then should not be counted for this operation.
        #
        if self.directive is None and not self.tokens:
            return indent

        if self.directive:
            out.write(str(self) + '\n')
        else:
            lines, indent = self.format_blocks(self.tokens, indent)
            for line in lines:
                out.write(line + '\n')

        return indent

    def __repr__(self):
        """Generate the representation of a given block."""
        if self.directive:
            result = "#%s " % self.directive
            if self.isIf():
                result += repr(self.expr)
            else:
                for tok in self.tokens:
                    result += repr(tok)
        else:
            result = ""
            for tok in self.tokens:
                result += repr(tok)

        return result

    def __str__(self):
        """Generate the string representation of a given block."""
        if self.directive:
            # "#if"
            if self.directive == "if":
                # small optimization to re-generate #ifdef and #ifndef
                e = self.expr.expr
                op = e[0]
                if op == "defined":
                    result = "#ifdef %s" % e[1]
                elif op == "!" and e[1][0] == "defined":
                    result = "#ifndef %s" % e[1][1]
                else:
                    result = "#if " + str(self.expr)

            # "#define"
            elif self.isDefine():
                result = "#%s %s" % (self.directive, self.define_id)
                if self.tokens:
                    result += " "
                expr = strip_space(' '.join([tok.id for tok in self.tokens]))
                # remove the space between name and '(' in function call
                result += re.sub(r'(\w+) \(', r'\1(', expr)

            # "#error"
            # Concatenating tokens with a space separator, because they may
            # not be quoted and broken into several tokens
            elif self.directive == "error":
                result = "#error %s" % ' '.join([tok.id for tok in self.tokens])

            else:
                result = "#%s" % self.directive
                if self.tokens:
                    result += " "
                result += ''.join([tok.id for tok in self.tokens])
        else:
            lines, _ = self.format_blocks(self.tokens)
            result = '\n'.join(lines)

        return result


class BlockList(object):
    """A convenience class used to hold and process a list of blocks.

    It calls the cpp parser to get the blocks.
    """

    def __init__(self, blocks):
        self.blocks = blocks

    def __len__(self):
        return len(self.blocks)

    def __getitem__(self, n):
        return self.blocks[n]

    def __repr__(self):
        return repr(self.blocks)

    def __str__(self):
        result = '\n'.join([str(b) for b in self.blocks])
        return result

    def dump(self):
        """Dump all the blocks in current BlockList."""
        print '##### BEGIN #####'
        for i, b in enumerate(self.blocks):
            print '### BLOCK %d ###' % i
            print b
        print '##### END #####'

    def optimizeIf01(self):
        """Remove the code between #if 0 .. #endif in a BlockList."""
        self.blocks = optimize_if01(self.blocks)

    def optimizeMacros(self, macros):
        """Remove known defined and undefined macros from a BlockList."""
        for b in self.blocks:
            if b.isIf():
                b.expr.optimize(macros)

    def optimizeAll(self, macros):
        self.optimizeMacros(macros)
        self.optimizeIf01()
        return

    def findIncludes(self):
        """Return the list of included files in a BlockList."""
        result = []
        for b in self.blocks:
            i = b.isInclude()
            if i:
                result.append(i)
        return result

    def write(self, out):
        indent = 0
        for b in self.blocks:
            indent = b.write(out, indent)

    def removeVarsAndFuncs(self, keep):
        """Remove variable and function declarations.

        All extern and static declarations corresponding to variable and
        function declarations are removed. We only accept typedefs and
        enum/structs/union declarations.

        However, we keep the definitions corresponding to the set of known
        static inline functions in the set 'keep', which is useful
        for optimized byteorder swap functions and stuff like that.
        """

        # NOTE: It's also removing function-like macros, such as __SYSCALL(...)
        # in uapi/asm-generic/unistd.h, or KEY_FIELD(...) in linux/bcache.h.
        # It could be problematic when we have function-like macros but without
        # '}' following them. It will skip all the tokens/blocks until seeing a
        # '}' as the function end. Fortunately we don't have such cases in the
        # current kernel headers.

        # state = 0 => normal (i.e. LN + spaces)
        # state = 1 => typedef/struct encountered, ends with ";"
        # state = 2 => var declaration encountered, ends with ";"
        # state = 3 => func declaration encountered, ends with "}"

        state = 0
        depth = 0
        blocks2 = []
        skipTokens = False
        for b in self.blocks:
            if b.isDirective():
                blocks2.append(b)
            else:
                n = len(b.tokens)
                i = 0
                if skipTokens:
                    first = n
                else:
                    first = 0
                while i < n:
                    tok = b.tokens[i]
                    tokid = tok.id
                    # If we are not looking for the start of a new
                    # type/var/func, then skip over tokens until
                    # we find our terminator, managing the depth of
                    # accolades as we go.
                    if state > 0:
                        terminator = False
                        if tokid == '{':
                            depth += 1
                        elif tokid == '}':
                            if depth > 0:
                                depth -= 1
                            if (depth == 0) and (state == 3):
                                terminator = True
                        elif tokid == ';' and depth == 0:
                            terminator = True

                        if terminator:
                            # we found the terminator
                            state = 0
                            if skipTokens:
                                skipTokens = False
                                first = i + 1

                        i += 1
                        continue

                    # Is it a new type definition, then start recording it
                    if tok.id in ['struct', 'typedef', 'enum', 'union',
                                  '__extension__']:
                        state = 1
                        i += 1
                        continue

                    # Is it a variable or function definition. If so, first
                    # try to determine which type it is, and also extract
                    # its name.
                    #
                    # We're going to parse the next tokens of the same block
                    # until we find a semicolon or a left parenthesis.
                    #
                    # The semicolon corresponds to a variable definition,
                    # the left-parenthesis to a function definition.
                    #
                    # We also assume that the var/func name is the last
                    # identifier before the terminator.
                    #
                    j = i + 1
                    ident = ""
                    while j < n:
                        tokid = b.tokens[j].id
                        if tokid == '(':  # a function declaration
                            state = 3
                            break
                        elif tokid == ';':  # a variable declaration
                            state = 2
                            break
                        if b.tokens[j].kind == TokenKind.IDENTIFIER:
                            ident = b.tokens[j].id
                        j += 1

                    if j >= n:
                        # This can only happen when the declaration
                        # does not end on the current block (e.g. with
                        # a directive mixed inside it.
                        #
                        # We will treat it as malformed because
                        # it's very hard to recover from this case
                        # without making our parser much more
                        # complex.
                        #
                        logging.debug("### skip unterminated static '%s'",
                                      ident)
                        break

                    if ident in keep:
                        logging.debug("### keep var/func '%s': %s", ident,
                                      repr(b.tokens[i:j]))
                    else:
                        # We're going to skip the tokens for this declaration
                        logging.debug("### skip var/func '%s': %s", ident,
                                      repr(b.tokens[i:j]))
                        if i > first:
                            blocks2.append(Block(b.tokens[first:i]))
                        skipTokens = True
                        first = n

                    i += 1

                if i > first:
                    #print "### final '%s'" % repr(b.tokens[first:i])
                    blocks2.append(Block(b.tokens[first:i]))

        self.blocks = blocks2

    def replaceTokens(self, replacements):
        """Replace tokens according to the given dict."""
        extra_includes = []
        for b in self.blocks:
            made_change = False
            if b.isInclude() is None:
                i = 0
                while i < len(b.tokens):
                    tok = b.tokens[i]
                    if (tok.kind == TokenKind.KEYWORD and tok.id == 'struct'
                        and (i + 2) < len(b.tokens) and b.tokens[i + 2].id == '{'):
                        struct_name = b.tokens[i + 1].id
                        if struct_name in kernel_struct_replacements:
                            extra_includes.append("<bits/%s.h>" % struct_name)
                            end = i + 2
                            while end < len(b.tokens) and b.tokens[end].id != '}':
                                end += 1
                            end += 1 # Swallow '}'
                            while end < len(b.tokens) and b.tokens[end].id != ';':
                                end += 1
                            end += 1 # Swallow ';'
                            # Remove these tokens. We'll replace them later with a #include block.
                            b.tokens[i:end] = []
                            made_change = True
                            # We've just modified b.tokens, so revisit the current offset.
                            continue
                    if tok.kind == TokenKind.IDENTIFIER:
                        if tok.id in replacements:
                            tok.id = replacements[tok.id]
                            made_change = True
                    i += 1

                if b.isDefine() and b.define_id in replacements:
                    b.define_id = replacements[b.define_id]
                    made_change = True

            if made_change and b.isIf():
                # Keep 'expr' in sync with 'tokens'.
                b.expr = CppExpr(b.tokens)

        for extra_include in extra_includes:
            replacement = CppStringTokenizer(extra_include)
            self.blocks.insert(2, Block(replacement.tokens, directive='include'))



def strip_space(s):
    """Strip out redundant space in a given string."""

    # NOTE: It ought to be more clever to not destroy spaces in string tokens.
    replacements = {' . ': '.',
                    ' [': '[',
                    '[ ': '[',
                    ' ]': ']',
                    '( ': '(',
                    ' )': ')',
                    ' ,': ',',
                    '# ': '#',
                    ' ;': ';',
                    '~ ': '~',
                    ' -> ': '->'}
    result = s
    for r in replacements:
        result = result.replace(r, replacements[r])

    # Remove the space between function name and the parenthesis.
    result = re.sub(r'(\w+) \(', r'\1(', result)
    return result


class BlockParser(object):
    """A class that converts an input source file into a BlockList object."""

    def __init__(self, tokzer=None):
        """Initialize a block parser.

        The input source is provided through a Tokenizer object.
        """
        self._tokzer = tokzer
        self._parsed = False

    @property
    def parsed(self):
        return self._parsed

    @staticmethod
    def _short_extent(extent):
        return '%d:%d - %d:%d' % (extent.start.line, extent.start.column,
                                  extent.end.line, extent.end.column)

    def getBlocks(self, tokzer=None):
        """Return all the blocks parsed."""

        def consume_extent(i, tokens, extent=None, detect_change=False):
            """Return tokens that belong to the given extent.

            It parses all the tokens that follow tokens[i], until getting out
            of the extent. When detect_change is True, it may terminate early
            when detecting preprocessing directives inside the extent.
            """

            result = []
            if extent is None:
                extent = tokens[i].cursor.extent

            while i < len(tokens) and tokens[i].location in extent:
                t = tokens[i]
                if debugBlockParser:
                    print ' ' * 2, t.id, t.kind, t.cursor.kind
                if (detect_change and t.cursor.extent != extent and
                    t.cursor.kind == CursorKind.PREPROCESSING_DIRECTIVE):
                    break
                result.append(t)
                i += 1
            return (i, result)

        def consume_line(i, tokens):
            """Return tokens that follow tokens[i] in the same line."""
            result = []
            line = tokens[i].location.line
            while i < len(tokens) and tokens[i].location.line == line:
                if tokens[i].cursor.kind == CursorKind.PREPROCESSING_DIRECTIVE:
                    break
                result.append(tokens[i])
                i += 1
            return (i, result)

        if tokzer is None:
            tokzer = self._tokzer
        tokens = tokzer.tokens

        blocks = []
        buf = []
        i = 0

        while i < len(tokens):
            t = tokens[i]
            cursor = t.cursor

            if debugBlockParser:
                print ("%d: Processing [%s], kind=[%s], cursor=[%s], "
                       "extent=[%s]" % (t.location.line, t.spelling, t.kind,
                                        cursor.kind,
                                        self._short_extent(cursor.extent)))

            if cursor.kind == CursorKind.PREPROCESSING_DIRECTIVE:
                if buf:
                    blocks.append(Block(buf))
                    buf = []

                j = i
                if j + 1 >= len(tokens):
                    raise BadExpectedToken("### BAD TOKEN at %s" % (t.location))
                directive = tokens[j+1].id

                if directive == 'define':
                    if i+2 >= len(tokens):
                        raise BadExpectedToken("### BAD TOKEN at %s" %
                                               (tokens[i].location))

                    # Skip '#' and 'define'.
                    extent = tokens[i].cursor.extent
                    i += 2
                    id = ''
                    # We need to separate the id from the remaining of
                    # the line, especially for the function-like macro.
                    if (i + 1 < len(tokens) and tokens[i+1].id == '(' and
                        (tokens[i].location.column + len(tokens[i].spelling) ==
                         tokens[i+1].location.column)):
                        while i < len(tokens):
                            id += tokens[i].id
                            if tokens[i].spelling == ')':
                                i += 1
                                break
                            i += 1
                    else:
                        id += tokens[i].id
                        # Advance to the next token that follows the macro id
                        i += 1

                    (i, ret) = consume_extent(i, tokens, extent=extent)
                    blocks.append(Block(ret, directive=directive,
                                        lineno=t.location.line, identifier=id))

                else:
                    (i, ret) = consume_extent(i, tokens)
                    blocks.append(Block(ret[2:], directive=directive,
                                        lineno=t.location.line))

            elif cursor.kind == CursorKind.INCLUSION_DIRECTIVE:
                if buf:
                    blocks.append(Block(buf))
                    buf = []
                directive = tokens[i+1].id
                (i, ret) = consume_extent(i, tokens)

                blocks.append(Block(ret[2:], directive=directive,
                                    lineno=t.location.line))

            elif cursor.kind == CursorKind.VAR_DECL:
                if buf:
                    blocks.append(Block(buf))
                    buf = []

                (i, ret) = consume_extent(i, tokens, detect_change=True)
                buf += ret

            elif cursor.kind == CursorKind.FUNCTION_DECL:
                if buf:
                    blocks.append(Block(buf))
                    buf = []

                (i, ret) = consume_extent(i, tokens, detect_change=True)
                buf += ret

            else:
                (i, ret) = consume_line(i, tokens)
                buf += ret

        if buf:
            blocks.append(Block(buf))

        # _parsed=True indicates a successful parsing, although may result an
        # empty BlockList.
        self._parsed = True

        return BlockList(blocks)

    def parse(self, tokzer):
        return self.getBlocks(tokzer)

    def parseFile(self, path):
        return self.getBlocks(CppFileTokenizer(path))


class BlockParserTests(unittest.TestCase):
    """BlockParser unit tests."""

    def get_blocks(self, lines):
        blocks = BlockParser().parse(CppStringTokenizer('\n'.join(lines)))
        return map(lambda a: str(a), blocks)

    def test_hash(self):
        self.assertEqual(self.get_blocks(["#error hello"]), ["#error hello"])

    def test_empty_line(self):
        self.assertEqual(self.get_blocks(["foo", "", "bar"]), ["foo bar"])

    def test_hash_with_space(self):
        # We currently cannot handle the following case with libclang properly.
        # Fortunately it doesn't appear in current headers.
        #self.assertEqual(self.get_blocks(["foo", "  #  ", "bar"]), ["foo", "bar"])
        pass

    def test_with_comment(self):
        self.assertEqual(self.get_blocks(["foo",
                                          "  #  /* ahah */ if defined(__KERNEL__) /* more */",
                                          "bar", "#endif"]),
                         ["foo", "#ifdef __KERNEL__", "bar", "#endif"])


################################################################################
################################################################################
#####                                                                      #####
#####        B L O C K   L I S T   O P T I M I Z A T I O N                 #####
#####                                                                      #####
################################################################################
################################################################################


def find_matching_endif(blocks, i):
    """Traverse the blocks to find out the matching #endif."""
    n = len(blocks)
    depth = 1
    while i < n:
        if blocks[i].isDirective():
            dir_ = blocks[i].directive
            if dir_ in ["if", "ifndef", "ifdef"]:
                depth += 1
            elif depth == 1 and dir_ in ["else", "elif"]:
                return i
            elif dir_ == "endif":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return i


def optimize_if01(blocks):
    """Remove the code between #if 0 .. #endif in a list of CppBlocks."""
    i = 0
    n = len(blocks)
    result = []
    while i < n:
        j = i
        while j < n and not blocks[j].isIf():
            j += 1
        if j > i:
            logging.debug("appending lines %d to %d", blocks[i].lineno,
                          blocks[j-1].lineno)
            result += blocks[i:j]
        if j >= n:
            break
        expr = blocks[j].expr
        r = expr.toInt()
        if r is None:
            result.append(blocks[j])
            i = j + 1
            continue

        if r == 0:
            # if 0 => skip everything until the corresponding #endif
            start_dir = blocks[j].directive
            j = find_matching_endif(blocks, j + 1)
            if j >= n:
                # unterminated #if 0, finish here
                break
            dir_ = blocks[j].directive
            if dir_ == "endif":
                logging.debug("remove 'if 0' .. 'endif' (lines %d to %d)",
                              blocks[i].lineno, blocks[j].lineno)
                if start_dir == "elif":
                    # Put an endif since we started with an elif.
                    result += blocks[j:j+1]
                i = j + 1
            elif dir_ == "else":
                # convert 'else' into 'if 1'
                logging.debug("convert 'if 0' .. 'else' into 'if 1' (lines %d "
                              "to %d)", blocks[i].lineno, blocks[j-1].lineno)
                if start_dir == "elif":
                    blocks[j].directive = "elif"
                else:
                    blocks[j].directive = "if"
                blocks[j].expr = CppExpr(CppStringTokenizer("1").tokens)
                i = j
            elif dir_ == "elif":
                # convert 'elif' into 'if'
                logging.debug("convert 'if 0' .. 'elif' into 'if'")
                if start_dir == "elif":
                    blocks[j].directive = "elif"
                else:
                    blocks[j].directive = "if"
                i = j
            continue

        # if 1 => find corresponding endif and remove/transform them
        k = find_matching_endif(blocks, j + 1)
        if k >= n:
            # unterminated #if 1, finish here
            logging.debug("unterminated 'if 1'")
            result += blocks[j+1:k]
            break

        start_dir = blocks[j].directive
        dir_ = blocks[k].directive
        if dir_ == "endif":
            logging.debug("convert 'if 1' .. 'endif' (lines %d to %d)",
                          blocks[j].lineno, blocks[k].lineno)
            if start_dir == "elif":
                # Add the elif in to the results and convert it to an elif 1.
                blocks[j].tokens = CppStringTokenizer("1").tokens
                result += blocks[j:j+1]
            result += optimize_if01(blocks[j+1:k])
            if start_dir == "elif":
                # Add the endif in to the results.
                result += blocks[k:k+1]
            i = k + 1
        elif dir_ == "else":
            # convert 'else' into 'if 0'
            logging.debug("convert 'if 1' .. 'else' (lines %d to %d)",
                          blocks[j].lineno, blocks[k].lineno)
            if start_dir == "elif":
                # Add the elif in to the results and convert it to an elif 1.
                blocks[j].tokens = CppStringTokenizer("1").tokens
                result += blocks[j:j+1]
            result += optimize_if01(blocks[j+1:k])
            if start_dir == "elif":
                blocks[k].directive = "elif"
            else:
                blocks[k].directive = "if"
            blocks[k].expr = CppExpr(CppStringTokenizer("0").tokens)
            i = k
        elif dir_ == "elif":
            # convert 'elif' into 'if 0'
            logging.debug("convert 'if 1' .. 'elif' (lines %d to %d)",
                          blocks[j].lineno, blocks[k].lineno)
            result += optimize_if01(blocks[j+1:k])
            blocks[k].expr = CppExpr(CppStringTokenizer("0").tokens)
            i = k
    return result

class OptimizerTests(unittest.TestCase):
    def parse(self, text, macros=None):
        out = utils.StringOutput()
        blocks = BlockParser().parse(CppStringTokenizer(text))
        blocks.replaceTokens(kernel_token_replacements)
        blocks.optimizeAll(macros)
        blocks.write(out)
        return out.get()

    def test_if1(self):
        text = """\
#if 1
#define  GOOD
#endif
"""
        expected = """\
#define GOOD
"""
        self.assertEqual(self.parse(text), expected)

    def test_if0(self):
        text = """\
#if 0
#define  SHOULD_SKIP1
#define  SHOULD_SKIP2
#endif
"""
        expected = ""
        self.assertEqual(self.parse(text), expected)

    def test_if1_else(self):
        text = """\
#if 1
#define  GOOD
#else
#define  BAD
#endif
"""
        expected = """\
#define GOOD
"""
        self.assertEqual(self.parse(text), expected)

    def test_if0_else(self):
        text = """\
#if 0
#define  BAD
#else
#define  GOOD
#endif
"""
        expected = """\
#define GOOD
"""
        self.assertEqual(self.parse(text), expected)

    def test_if_elif1(self):
        text = """\
#if defined(something)
#define EXISTS
#elif 1
#define GOOD
#endif
"""
        expected = """\
#ifdef something
#define EXISTS
#elif 1
#define GOOD
#endif
"""
        self.assertEqual(self.parse(text), expected)

    def test_if_elif1_macro(self):
        text = """\
#if defined(something)
#define EXISTS
#elif defined(WILL_BE_ONE)
#define GOOD
#endif
"""
        expected = """\
#ifdef something
#define EXISTS
#elif 1
#define GOOD
#endif
"""
        self.assertEqual(self.parse(text, {"WILL_BE_ONE": "1"}), expected)


    def test_if_elif1_else(self):
        text = """\
#if defined(something)
#define EXISTS
#elif 1
#define GOOD
#else
#define BAD
#endif
"""
        expected = """\
#ifdef something
#define EXISTS
#elif 1
#define GOOD
#endif
"""
        self.assertEqual(self.parse(text), expected)

    def test_if_elif1_else_macro(self):
        text = """\
#if defined(something)
#define EXISTS
#elif defined(WILL_BE_ONE)
#define GOOD
#else
#define BAD
#endif
"""
        expected = """\
#ifdef something
#define EXISTS
#elif 1
#define GOOD
#endif
"""
        self.assertEqual(self.parse(text, {"WILL_BE_ONE": "1"}), expected)


    def test_if_elif1_else_macro(self):
        text = """\
#if defined(something)
#define EXISTS
#elif defined(WILL_BE_ONE)
#define GOOD
#else
#define BAD
#endif
"""
        expected = """\
#ifdef something
#define EXISTS
#elif 1
#define GOOD
#endif
"""
        self.assertEqual(self.parse(text, {"WILL_BE_ONE": "1"}), expected)

    def test_macro_set_to_undefined_single(self):
        text = """\
#if defined(__KERNEL__)
#define BAD_KERNEL
#endif
"""
        expected = ""
        macros = {"__KERNEL__": kCppUndefinedMacro}
        self.assertEqual(self.parse(text, macros), expected)

    def test_macro_set_to_undefined_if(self):
        text = """\
#if defined(__KERNEL__) || !defined(__GLIBC__) || (__GLIBC__ < 2)
#define CHECK
#endif
"""
        expected = """\
#if !defined(__GLIBC__) || __GLIBC__ < 2
#define CHECK
#endif
"""
        macros = {"__KERNEL__": kCppUndefinedMacro}
        self.assertEqual(self.parse(text, macros), expected)

    def test_endif_comment_removed(self):
        text = """\
#ifndef SIGRTMAX
#define SIGRTMAX 123
#endif /* SIGRTMAX */
"""
        expected = """\
#ifndef __SIGRTMAX
#define __SIGRTMAX 123
#endif
"""
        self.assertEqual(self.parse(text), expected)

    def test_multilevel_if0(self):
        text = """\
#if 0
#if 1
#define  BAD_6
#endif
#endif
"""
        expected = ""
        self.assertEqual(self.parse(text), expected)


if __name__ == '__main__':
    unittest.main()
