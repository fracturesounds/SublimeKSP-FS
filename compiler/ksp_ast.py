# ksp-compiler - a compiler for the Kontakt script language
# Copyright (C) 2011  Nils Liberg
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version:
# http://www.gnu.org/licenses/gpl-2.0.html
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

import sys, types
import ply.lex as lex
import copy
from decimal import Decimal
from ksp_builtins import functions_with_forced_parentheses

precedence = {  '&' :  0,
                'or':  1,
                'xor': 2,
                'and': 3,
                'not': 4,
                '=': 5, '<': 5, '>': 5, '<=': 5, '>=': 5, '#': 5,
                '.or.': 6,
                '.xor.': 7,
                '.and.': 8,
                '.not.': 9,
                '+': 10, '-': 10,
                '*': 11, '/': 11, 'mod': 11,
                'unary-': 12,
                }

def toint(i, bits=32):
    ' converts to a signed integer with <bits> bits '
    i &= (1 << bits) - 1       # get last "bits" bits, as unsigned

    if i & (1 << (bits - 1)):  # if negative in N-bit 2's comp
        i -= 1 << bits         # ... make it negative

    return int(i)

class Emitter:
    '''Class for converting Nodes to native KSP'''

    def __init__(self, out=sys.stdout, compact=False, compiled_code_tab_size=2):
        self.out = out
        self.indent_num = 0
        self.beginning_of_line = True
        self.compact = compact
        self.compiled_code_tab_size = compiled_code_tab_size

    def indent(self):
        self.indent_num += self.compiled_code_tab_size

    def dedent(self):
        self.indent_num -= self.compiled_code_tab_size

    def _write_string(self, s):
        lines = s.split('\n')

        if self.compact:
            indent = ''
        else:
            indent = ' ' * self.indent_num
        for (i, line) in enumerate(lines):

            if line:
                if self.beginning_of_line:
                    self.out.write(indent)

                self.out.write(line)

            if i < len(lines)-1:
                self.out.write('\n')
                self.beginning_of_line = True
            elif line:
                self.beginning_of_line = False

    def write(self, *args, **kwargs):
        indented = kwargs.get('indented', False)

        if indented:
            self.indent()
        try:
            for arg in args:
                if isinstance(arg, str):
                    self._write_string(arg)
                elif isinstance(arg, list):
                    self.write(*arg)
                elif hasattr(arg, 'emit'):
                    arg.emit(self)
                else:
                    self._write_string(str(arg))
        finally:
            if indented:
                self.dedent()

    def writeln(self, *args, **kwargs):
        self.write(*args, **kwargs)
        self.write('\n')

class ParseException(SyntaxError):
    '''Parse Exceptions for parse errors after AST lex/yacc parsing'''

    def __init__(self, node, msg=None):
        if msg is None:
            msg = 'Syntax Error'

        if node:
            lineno = node.lineno

            if lineno == 0 and hasattr(node, 'get_childnodes'):
                for child in node.get_childnodes():
                    if child.lineno != 0:
                        lineno = child.lineno
                        break

            self.lineno = lineno
            self.node = node
            self.msg = msg

            SyntaxError.__init__(self, msg)
        else:
            SyntaxError.__init__(self, msg)

class ASTNode:
    '''The very base node comprised in all AST objects'''

    def __init__(self, lexinfo):
        self.lexinfo = None
        self.env = None

        if lexinfo:
            if type(lexinfo) is tuple:
                self.lexinfo = lexinfo
            else:
                line_filename = lexinfo.lexer.lines[lexinfo.lineno(1)].locations[0][0]
                line_namespaces = lexinfo.lexer.lines[lexinfo.lineno(1)].namespaces # Add namespaces as lexinfo

                if line_filename is None:
                    # the penultimate element is a list of function nodes related to inlining of functions
                    self.lexinfo = (lexinfo.lexer.filename, lexinfo.lineno(1), [], None)
                else:
                    self.lexinfo = (line_filename, lexinfo.lineno(1), [], line_namespaces)
        else:
            raise Exception('Missing lexinfo!')

    @property
    def lineno(self):
        return self.lexinfo[1]

    def copy(self):
        return copy.deepcopy(self)

    def put_symbol(self, name, value):
        self.env.put(name, value)

    def get_childnodes(self):
        pass

    def __str__(self):
        children = [c.__class__.__name__ for c in self.get_childnodes()]
        s = str(self.__class__.__name__)

        if children and not isinstance(self, CompoundStmt) \
                    and not isinstance(self, Expr)         \
                    and not isinstance(self, Module)       \
                    and not isinstance(self, TopLevelBlock):
            s = s + '(%s)' % ','.join(children)

        return s

    def __repr__(self):
        return '<%s>' % (str(self))

    def node_type(self):
        return self.__class__.__name__

class Module(ASTNode):
    '''Parent Node for all nodes within script'''

    def __init__(self, lexinfo, blocks):
        ASTNode.__init__(self, lexinfo)
        self.blocks = blocks

    def emit(self, out):
        for block in self.blocks:
            block.emit(out)

            if not out.compact:
                out.write('\n')

    def copy_tree(self):
        Module()

    def get_childnodes(self):
        return self.blocks

class TopLevelBlock(ASTNode):
    '''Parent node for: Imports (deprecated), Functions & Callbacks'''

    def __init__(self, lexinfo, name, lines=None):
        ASTNode.__init__(self, lexinfo)
        self.name = name

        if lines is None:
            self.lines = []
        else:
            self.lines = lines[:]

    def get_childnodes(self):
        return self.lines # NOTE: name?

class Import(TopLevelBlock):
    '''(Deprecated) Imports are now evaluated before AST construction so this is now deprecated'''

    def __init__(self, lexinfo, filename, alias=None):
        TopLevelBlock.__init__(self, lexinfo, 'import', [])
        self.filename = filename
        self.alias = alias

    def emit(self, out):
        out.write("import '%s'" % self.filename)

        if self.alias:
            out.write('as %s' % self.alias)

        out.writeln()

    def get_childnodes(self):
        return []

class FunctionDef(TopLevelBlock):
    '''Node for Functions and Taskfuncs'''

    def __init__(self, lexinfo, name, parameters, return_value, lines, is_taskfunc=False, override=False):
        TopLevelBlock.__init__(self, lexinfo, name, lines)
        self.name = name
        self.parameters = []
        self.parameter_types = []

        for p in parameters:
            if type(p) is tuple:
                param = p[1]
                param_type = str(p[0])
            else:
                param = p
                param_type = ''

            self.parameters.append(param)
            self.parameter_types.append(param_type)

        if return_value:
            self.parameter_types.append('out')

        self.return_value = return_value

        self.is_taskfunc = is_taskfunc
        self.override = override

    def get_childnodes(self):
        children = []
        children.append(self.name)
        children.extend(self.lines)

        return children

    def emit(self, out):
        out.write('function ', self.name)

        if self.parameters:
            out.write('(%s)' % ', '.join((str(p) for p in self.parameters)))

        out.writeln()
        out.write(self.lines, indented=True)
        out.writeln('end function')

class Callback(TopLevelBlock):
    '''Node for Callbacks (including UI)'''

    def __init__(self, lexinfo, name, lines=None, variable=None):
        TopLevelBlock.__init__(self, lexinfo, name, lines)
        self.variable = None

        if variable:
            self.variable = ID(lexinfo, variable)

    def get_childnodes(self):
        children = []

        if self.variable:
            children.append(self.variable)

        children.extend(self.lines)

        return children

    def emit(self, out):
        if self.variable:
            out.writeln('on %s(%s)' % (self.name, str(self.variable)))
        else:
            out.writeln('on %s' % self.name)

        out.write(self.lines, indented=True)
        out.writeln('end on')

class Stmt(ASTNode):
    '''Parent node for: PropertyDef, DeclareStmt, AssignStmt, PreprocessorCondition (deprecated), FunctionCall, CompoundStmt'''

    def __init__(self, lexinfo):
        ASTNode.__init__(self, lexinfo)

    def map_expr(self, func):
        pass

class PropertyDef(Stmt):
    '''Node for property statments'''

    def __init__(self, lexinfo, name, indices=None, functions=None, alias_varref=None):
        Stmt.__init__(self, lexinfo)
        self.name = name

        if alias_varref is not None:
            # if there is an alias varref automatically add get/set functions and use the indices as parameters
            functions = []

            # result := alias_varref
            functions.append(FunctionDef(lexinfo, ID(lexinfo, 'get'), parameters=indices, return_value=ID(lexinfo, 'result'),
                                         lines=[AssignStmt(lexinfo, VarRef(lexinfo, ID(lexinfo, 'result')), alias_varref)]))
            # alias_varref := value_to_set
            functions.append(FunctionDef(lexinfo, ID(lexinfo, 'set'), parameters=indices + [ID(lexinfo, 'value_to_set')], return_value=None,
                                         lines=[AssignStmt(lexinfo, alias_varref, VarRef(lexinfo, ID(lexinfo, 'value_to_set')))]))
        self.get_func_def = None
        self.set_func_def = None
        self.functiondefs = {}

        for func in functions:
            function_name = str(func.name)
            self.functiondefs[function_name] = func

            if function_name == 'get':
                self.get_func_def = func

            if function_name == 'set':
                self.set_func_def = func

    def get_childnodes(self):
        return []

class DeclareStmt(Stmt):
    '''Node for variable declarations (including UI)'''

    def __init__(self, lexinfo, variable, modifiers, size=None, parameters=None, initial_value=None):
        Stmt.__init__(self, lexinfo)
        self.variable = variable
        self.modifiers = modifiers
        self.size = size
        self.parameters = parameters or []
        self.initial_value = initial_value

    def isUIDeclaration(self):
        return any([m for m in self.modifiers if m.startswith('ui_')])

    def emit(self, out):
        out.write('declare ')

        if self.modifiers:
            out.write(' '.join(self.modifiers), ' ')

        out.write(str(self.variable))

        if self.size:
            out.write('[%s]' % self.size)

        if self.parameters:
            out.write('(%s)' % ','.join((str(p) for p in self.parameters)))

        if self.initial_value:
            out.write(' := ')

            if isinstance(self.initial_value, RawArrayInitializer):
                initial_value = self.initial_value.raw_text.split(',')
            else:
                initial_value = self.initial_value

            if type(initial_value) is list:
                out.write('(')

                for i, value in enumerate(initial_value):
                    out.write(str(value))

                    if i != len(initial_value) - 1:  # unless last element
                        # emit comma or newline+comma
                        values_per_line = 40

                        if i % values_per_line == 0 and i > 0:
                            out.write(', ...\n')
                        else:
                            out.write(', ')

                out.write(')')
            else:
                out.write(str(initial_value))

        out.writeln()

    def get_childnodes(self):
        children = []
        children.append(self.variable)

        if self.size:
            children.append(self.size)

        if self.initial_value:
            if type(self.initial_value) is list:
                children.extend(self.initial_value)
            else:
                children.append(self.initial_value)

        children.extend(self.parameters)

        return children

    def map_expr(self, func):
        self.variable = func(self.variable)
        self.size = func(self.size)
        self.parameters = [func(p) for p in self.parameters]
        self.initial_value = func(self.initial_value)

    def __repr__(self):
        return '<DeclareStmt %s>' % (str(self.variable.identifier))

class AssignStmt(Stmt):
    '''Node for assign statements. Separate to DeclareStmt as ':=' can be used elsewhere'''

    def __init__(self, lexinfo, varref, expression):
        Stmt.__init__(self, lexinfo)
        self.varref = varref
        self.expression = expression

    def emit(self, out):
        out.write(self.varref)
        out.write(' := ')
        out.writeln(self.expression)

    def map_expr(self, func):
        self.varref = func(self.varref)
        self.expression = func(self.expression)

    def get_childnodes(self):
        return (self.varref, self.expression)

class PreprocessorCondition(Stmt):
    '''(Deprecated) SET_CONDITION and RESET_CONDITION are now handled before AST lex/yacc parsing'''

    def __init__(self, lexinfo, set_or_reset_name, parameter):
        Stmt.__init__(self, lexinfo)
        self.set_or_reset_name = set_or_reset_name
        self.parameter = parameter

    def __str__(self):
        return '%s(%s)\n' % (self.set_or_reset_name, self.parameter)

    def emit(self, out):
        out.write(str(self))

    def get_childnodes(self):
        return []

class FunctionCall(Stmt):
    '''Node for called functions'''

    def __init__(self, lexinfo, function_name, parameters, is_procedure=False, using_call_keyword=False):
        Stmt.__init__(self, lexinfo)
        self.function_name = function_name
        self.parameters = parameters
        self.is_procedure = is_procedure
        self.using_call_keyword = using_call_keyword

    def __str__(self):
        s = str(self.function_name)

        if self.parameters or not self.is_procedure or s in functions_with_forced_parentheses:
            s += '(%s)' % ','.join((str(p) for p in self.parameters))

        if self.is_procedure:
            s += '\n'

        return s

    def emit(self, out):
        if self.using_call_keyword:
            out.write('call ')

        out.write(str(self))

    def map_expr(self, func):
        self.parameters = [func(p) for p in self.parameters]

    def get_childnodes(self):
        children = []
        children.extend(self.parameters)
        children.append(self.function_name)

        return children

class CompoundStmt(Stmt):
    '''Parent Node for: WhileStmt, ForStmt, FamilyStmt, IfStmt, SelectStmt'''
    pass

class WhileStmt(CompoundStmt):
    '''Node for while loops'''

    def __init__(self, lexinfo, condition, statements):
        CompoundStmt.__init__(self, lexinfo)
        self.condition = condition
        self.statements = statements

    def emit(self, out):
        out.writeln('while (', self.condition, ')')
        out.write(self.statements, indented=True)
        out.writeln('end while')

    def map_expr(self, func):
        self.condition = func(self.condition)

    def get_childnodes(self):
        return (self.condition,) + tuple(self.statements)

class ForStmt(CompoundStmt):
    '''Node for for loops '''

    def __init__(self, lexinfo, loopvar, start, end, statements, step=None, downto=False):
        CompoundStmt.__init__(self, lexinfo)
        self.loopvar = loopvar
        self.start = start
        self.end = end
        self.statements = statements
        self.step = step
        self.downto = downto

    def emit(self, out):
        if self.downto:
            toword = 'downto'
        else:
            toword = 'to'

        out.writeln('for %s := %s %s %s' % (self.loopvar, self.start, toword, self.end))
        out.write(self.statements, indented=True)
        out.writeln('end for')

    def map_expr(self, func):
        self.loopvar = func(self.loopvar)
        self.start = func(self.start)
        self.end = func(self.end)

        if self.step:
            self.step = func(self.step)

    def get_childnodes(self):
        children = []
        children.append(self.loopvar)
        children.append(self.start)
        children.append(self.end)

        if self.step:
            children.append(self.step)

        children.extend(self.statements)

        return children

class FamilyStmt(CompoundStmt):
    '''Node for families'''

    def __init__(self, lexinfo, name, statements):
        CompoundStmt.__init__(self, lexinfo)
        self.name = name
        self.statements = statements

    def emit(self, out):
        out.write('family %s\n' % self.name)
        out.write(self.statements, indented=True)
        out.write('end family\n')

    def get_childnodes(self):
        return tuple(self.statements)

class IfStmt(CompoundStmt):
    '''Node for if statments'''

    def __init__(self, lexinfo, condition_stmts_tuples):
        CompoundStmt.__init__(self, lexinfo)

        # list of (condition, statement-list) tuples. In the case of just "else", the condition will be None.
        self.condition_stmts_tuples = condition_stmts_tuples

    def emit(self, out):
        num_ends = 0

        for (i, (condition, stmts)) in enumerate(self.condition_stmts_tuples):
            if condition and i == 0:
                out.writeln('if (', condition, ')')
                num_ends += 1
            elif condition:
                out.writeln('else if (', condition, ')')
            else:
                out.writeln('else')

            out.write(stmts, indented=True)

        for i in range(num_ends):
            out.writeln('end if')

    def get_childnodes(self):
        children = []

        for (condition, stmts) in self.condition_stmts_tuples:
            if condition:
                children.append(condition)

            children.extend(stmts)

        return children

    def map_expr(self, func):
        self.condition_stmts_tuples = [(func(condition), stmts) for (condition, stmts) in self.condition_stmts_tuples]

class SelectStmt(CompoundStmt):
    '''Node for select statements'''

    def __init__(self, lexinfo, expression, range_stmts_tuples):
        CompoundStmt.__init__(self, lexinfo)
        self.expression = expression

        # list of ((min-value, max-value), statement-list) tuples. Please note that max-value may be None.
        self.range_stmts_tuples = range_stmts_tuples

    def emit(self, out):
        out.writeln('select (', self.expression, ')')

        for ((start, stop), stmts) in self.range_stmts_tuples:
            try:
                out.indent()

                if stop is None:
                    out.writeln('case %s' % start)
                else:
                    out.writeln('case %s to %s' % (start, stop))

                out.write(stmts, indented=True)
            finally:
                out.dedent()

        out.writeln('end select')

    def get_childnodes(self):
        children = []
        children.append(self.expression)

        for ((start, stop), stmts) in self.range_stmts_tuples:
            children.append(start)

            if stop:
                children.append(stop)

            children.extend(stmts)

        return children

    def map_expr(self, func):
        self.expression = func(self.expression)
        self.range_stmts_tuples = [((func(start), func(stop)), stmts) for ((start, stop), stmts) in self.range_stmts_tuples]

class Expr(ASTNode):
    '''Parent node for: BinOp, UnaryOp, Integer, Real, String, Boolean, ID, VarRef, RawArrayInitializer'''

    def __init__(self, lexinfo):
        ASTNode.__init__(self, lexinfo)

class BinOp(Expr):
    '''Node for binary operators e.g 4 < 5'''

    def __init__(self, lexinfo, left, op, right):
        Expr.__init__(self, lexinfo)
        self.left = left
        self.right = right
        self.op = op

    def __str__(self):
        l = str(self.left)
        r = str(self.right)

        if isinstance(self.left, BinOp) and precedence[self.op] > precedence[self.left.op]:
            l = '(%s)' % l

        if isinstance(self.right, BinOp) and not (self.right.op == self.op and self.op in '+&'):
            r = '(%s)' % r

        if self.op in '+-*/ = < > >= <=':
            return '%s%s%s' % (l, self.op, r)
        else:
            return '%s %s %s' % (l, self.op, r)

    def get_childnodes(self):
        return (self.left, self.right)

class UnaryOp(Expr):
    '''Node for Unary operators e.g not'''

    def __init__(self, lexinfo, op, right):
        Expr.__init__(self, lexinfo)
        self.right = right
        self.op = op

    def __str__(self):
        # special case since this number can only be represented in hex due to a Kontakt bug
        if self.op == '-' and isinstance(self.right, Integer) and self.right.value == -2147483648:
            return '080000000h'

        r = str(self.right)

        if self.op == '-':
            op_prefix = '-'
        else:
            op_prefix = self.op + ' '

        if isinstance(self.right, BinOp):
            r = '(%s)' % r

        return op_prefix + r

    def get_childnodes(self):
        return (self.right,)

class Integer(Expr):
    '''Node for integers'''

    def __init__(self, lexinfo, value):
        Expr.__init__(self, lexinfo)
        self.value = toint(value)

    def __str__(self):
        # special case since this number can only be represented in hex due to a Kontakt bug
        if self.value == -2147483648:
            return '080000000h'
        else:
            return str(self.value)

    def get_childnodes(self):
        return ()

class Real(Expr):
    '''Node for real numbers'''

    def __init__(self, lexinfo, value):
        Expr.__init__(self, lexinfo)
        self.value = Decimal(value)

    def __str__(self):
        # borrowed from "Karin" on Stack Overflow, a Duolingo software engineer
        f = float(self.value)
        float_string = repr(f)

        # detect scientific notation
        if 'e' in float_string:
            digits, exp = float_string.split('e')
            digits = digits.replace('.', '').replace('-', '')
            exp = int(exp)
            zero_padding = '0' * (abs(int(exp)) - 1)  # - 1 for decimal point in the sci notation
            sign = '-' if f < 0 else ''

            if exp > 0:
                float_string = '{}{}{}.0'.format(sign, digits, zero_padding)
            else:
                float_string = '{}0.{}{}'.format(sign, zero_padding, digits)

        return float_string

    def get_childnodes(self):
        return ()

class String(Expr):
    '''Node for strings'''

    def __init__(self, lexinfo, value):
        Expr.__init__(self, lexinfo)
        self.value = value

    def get_childnodes(self):
        return ()

    def __str__(self):
        return str(self.value)

class Boolean(Expr):
    '''Node for boolean operators  \n
       Note: KSP doesn't support booleans, but this node type is used as an intermediary in the optimization phase
    '''

    def __init__(self, lexinfo, value):
        Expr.__init__(self, lexinfo)
        self.value = bool(value)

    def __str__(self):
        if self.value:
            return '9=9'
        else:
            return '9=0'

    def get_childnodes(self):
        return ()

class ID(Expr):
    def __init__(self, lexinfo, identifier):
        Expr.__init__(self, lexinfo)
        if identifier[0] in '$%@!?~':
            self.set_identifier(identifier[1:])
            self.prefix = identifier[0]
        else:
            self.set_identifier(identifier)
            self.prefix = ''

    def get_identifier(self):
        return self._identifier

    def set_identifier(self, identifier):
        self._identifier = str(identifier)
        sys.intern(self._identifier)

        # identifier_first_part represents the part of the name in front of the first dot (if any), eg. for myfamily.myvar it would represent myfamily
        if '.' in identifier:
            self.identifier_first_part = identifier[:identifier.index('.')]
            self.identifier_last_part  = identifier[identifier.index('.'):]
        else:
            self.identifier_first_part = identifier
            self.identifier_last_part = ''

    identifier = property(get_identifier, set_identifier)

    def copy(self, different_name=None):
        if different_name is None:
            id = ID(self.lexinfo, self.prefix + self.identifier)
        else:
            id = ID(self.lexinfo, different_name)

        return id

    def __hash__(self):
        return hash(self.identifier)

    def __str__(self):
        return '%s%s' % (self.prefix, self.identifier)

    def __repr__(self):
        return '%s%s' % (self.prefix, self.identifier)

    def get_childnodes(self):
        return ()

class VarRef(Expr):
    def __init__(self, lexinfo, identifier, subscripts=None):
        Expr.__init__(self, lexinfo)

        if subscripts is None:
            subscripts = []

        self.identifier = identifier
        self.subscripts = subscripts

    def __str__(self):
        if self.subscripts:
            return '%s[%s]' % (str(self.identifier), ','.join(str(s) for s in self.subscripts))
        else:
            return '%s'     % (str(self.identifier),)

    def get_childnodes(self):
        children = []
        children.append(self.identifier)
        children.extend(self.subscripts)

        return children

class RawArrayInitializer(Expr):
    '''Node for raw array initialization'''

    def __init__(self, lexinfo, raw_text):
        Expr.__init__(self, lexinfo)
        self.raw_text = raw_text

    def __str__(self):
        return self.raw_text

    def get_childnodes(self):
        return []
