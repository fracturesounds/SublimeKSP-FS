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

import re
import io
import os
import copy
import collections
from collections import OrderedDict
import ksp_ast
import ksp_ast_processing
from ksp_compiler_extras import flatten
import ksp_compiler_extras as comp_extras
import ksp_builtins
from ksp_parser import parse
from taskfunc import taskfunc_code
import hashlib
import ply.lex as lex
import time
import json
import utils

variable_prefixes = '$%@!?~'

# regular expressions:
white_space_re = r'(\s*(\{[^\n]*?\})?\s*)'
white_space = r'(?ms)%s' % white_space_re

comment_singleline_re = re.compile(r'''
            (?<!["\'])         # negative lookbehind to exclude comments inside strings
            \/\/.*             # match single-line comment //
            ''', re.VERBOSE)

comment_re = re.compile(r'''
    (?<!["'])   # negative lookbehind to make sure there is no quote before the pattern
    \{.*?\}     # match {...}
    |           # or
    \(\*.*?\*\) # match (*...*)
    |           # or
    /\*.*?\*/   # match /*...*/
''', re.DOTALL | re.VERBOSE)

string_re = re.compile(r'''
    "               # match a double quote
    .*?             # match any character (non-greedy)
    (?<!\\)"        # ensure the match is not preceded by a backslash
    |               # or
    '               # match a single quote
    .*?             # match any character (non-greedy)
    (?<!\\)'        # ensure the match is not preceded by a backslash
''', re.DOTALL | re.VERBOSE)

line_continuation_re = re.compile(r'''
    \.\.\.          # match ellipsis
    \s*             # match zero or more whitespace characters
    \n              # match newline character
''', re.MULTILINE | re.VERBOSE)  # End of regex pattern with flags

placeholder_re = re.compile(r'''
    \[\[\[    # match the opening sequence [[[
    \d+       # match one or more digits
    \]\]\]    # match the closing sequence ]]]
''', re.VERBOSE)

varname_re = re.compile(r'''
    (
        (\b|[$%!@~?])       # word boundary or special characters
        [0-9]*              # zero or more digits
        [a-zA-Z_]           # start with an alphabet or underscore
        [a-zA-Z0-9_]*       # followed by alphanumeric or underscore
        (\.[a-zA-Z_0-9]+)*  # zero or more occurrences of dot followed by alphanumeric or underscore
    )
    \b                      # word boundary
''', re.VERBOSE)

varname_dot_re = re.compile(r'''
    (?<![$%!@~?])            # negative lookbehind assertion
    \b                       # word boundary
    [0-9]*                   # any number of digits
    [a-zA-Z_][a-zA-Z0-9_]*?  # a word starting with a letter or underscore, followed by any number of letters, digits or underscores (lazy match)
    \.                       # a literal dot
''', re.VERBOSE)

compiler_options = '(remove_whitespace|compact_variables|combine_callbacks|extra_syntax_checks|optimize_code|extra_branch_optimization|add_compile_date|sanitize_exit_command|write_log_on_fail)'

pragma_compile_with_re = re.compile(r'\{\s*\#pragma\s+compile_with\s+%s\s*\}' % compiler_options)
pragma_compile_without_re = re.compile(r'\{\s*\#pragma\s+compile_without\s+%s\s*\}' % compiler_options)

import_re = re.compile(r'''
    ^\s*                                    # match start of line and any leading whitespace
    import\s+                               # match 'import' keyword and one or more whitespace characters
    "                                       # match opening double-quote character
    (?P<filename>                           # start a named capturing group 'filename'
        .+?                                 # match any character one or more times, non-greedily
    )                                       # end named capturing group 'filename'
    "                                       # match closing double-quote character
    (                                       # start a non-named capturing group
        \s+as\s+                            # match 'as' keyword surrounded with whitespace
        (?P<asname>[a-zA-Z_][a-zA-Z0-9_.]*) # match valid identifier as a named capturing group 'asname'
    )?                                      # end non-named capturing group and make it optional
    \s*                                     # match any trailing whitespace after the statement
    %s                                      # match any amount of whitespace and newlines after the statement
    $                                       # match the end of the line
''' % white_space_re, re.MULTILINE | re.DOTALL | re.VERBOSE)

import_basic_re = re.compile(r'^\s*import ')
import_ignore_re = re.compile(r'^\s*__IGNORE__')
macro_start_re = re.compile(r'^\s*macro(?=\W)')
macro_end_re = re.compile(r'^\s*end\s+macro')


placeholders            = {}            # mapping from placeholder number to contents (placeholders used for comments, strings, etc.)
functions               = OrderedDict() # maps from function names (prefixed with namespaces) to AST node corresponding to the function definition
functions_before_prefix = OrderedDict() # maps from function names to AST node corresponding to the function definition
variables               = set()         # a set of the names of the declared variables (prefixed with $, %, !, ? or @)
ui_variables            = set()         # a set of the names of the declared variables of UI type, like ui_knob, ui_value_edit, etc. (prefixed with $, %, !, ? or @)
families                = set()         # a set of the family names (prefixed with namespaces)
properties              = set()         # a set of the property names
functions_invoking_wait = set()         # a set functions containing the wait function
true_conditions         = set()         # the conditions set using SET_CONDITION
called_functions        = set()         # functions that are somewhere in the script invoked using the Kontakt 4.1 "call" keyword
call_graph = collections.defaultdict(list)  # an item (a, b) is included if function a invokes function b using the "call" keyword

def clear_global_context():
    placeholders.clear()
    functions.clear()
    functions_before_prefix.clear()
    variables.clear()
    ui_variables.clear()
    families.clear()
    properties.clear()
    functions_invoking_wait.clear()
    true_conditions.clear()
    called_functions.clear()
    call_graph.clear()


class StringIO:
    '''Simple class to work around the problem that cStringIO cannot handle certain Unicode input'''

    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    def getvalue(self):
        return ''.join(self.parts)

def append_overloaded_name(name, params):
    '''Returns name with number of arguments appended'''
    name = name + "__" + str(len(params))
    return name

def prefix_with_ns(name, namespaces, function_parameter_names = None, force_prefixing = False):
    '''Returns prefixed name'''

    if not namespaces:
        return name

    function_parameter_names = function_parameter_names or []

    if name[0] in variable_prefixes:
        prefix, unprefixed_name = name[0], name[1:]
    else:
        prefix, unprefixed_name = '', name

    # if the name consists of multiple parts (eg. myfamily.myvariable extract the first part - myfamily in this example)
    first_name_part = name.split('.')[0]

    # if built-in name or function parameter
    if (unprefixed_name in ksp_builtins.all_builtins_unprefixed or
          name in ksp_builtins.functions and not name in functions_before_prefix or
          name in ksp_builtins.keywords or
          first_name_part in function_parameter_names or
          name in ksp_builtins.sKSP_preprocessor_variables) and not force_prefixing:
        return name   # don't add prefix

    # add namespace to name
    return prefix + '.'.join(namespaces + [unprefixed_name])

def prefix_ID_with_ns(id, namespaces, function_parameter_names = None, force_prefixing = False):
    if namespaces:
        return ksp_ast.ID(id.lexinfo, identifier = prefix_with_ns(str(id), namespaces, function_parameter_names, force_prefixing))
    else:
        return id

class ExceptionWithMessage(Exception):
    _message = None

    def _get_message(self):
        return self._message

    def _set_message(self, value):
        self._message = value

    message = property(_get_message, _set_message)

class ParseException(ExceptionWithMessage):
    '''Parse Exceptions for parse errors raised before AST lex/yacc parsing'''

    def __init__(self, line, message, no_traceback = False):
        if no_traceback:
            utils.disable_traceback()

        if line.calling_lines:
            macro_chain = '\n'.join(['=> {}'.format(l.command.strip()) for l in line.calling_lines])
            line_content = 'Macro traceback:\n{}\n{}'.format(macro_chain, str(line).strip())
        else:
            line_content = str(line).strip()

        msg = "%s\n\n%s\n\n%s" % (message, line_content, line.get_locations_string())

        Exception.__init__(self, msg)
        self.line = line
        self.message = msg

class Line:
    '''Line object used for handling lines before AST lex/yacc parsing'''

    def __init__(self, s, locations = None, namespaces = None, placeholders = placeholders, calling_lines = None):
        # locations should be a list of (filename, lineno) tuples
        self.command = s # current line returned as string
        self.locations = locations or [(None, -1)] # filename and line number
        self.namespaces = namespaces or []   # a list of the namespaces (each import appends the as-name onto the stack)
        self.placeholders = placeholders
        self.source_locations = None
        self.calling_lines = calling_lines

    def get_lineno(self):
        return self.locations[0][1]

    def get_filename(self):
        return self.locations[0][0]

    lineno   = property(get_lineno)
    filename = property(get_filename)

    def get_locations_string(self):
        return '\n'.join(('%s%s: %d' % (' ' * (i * 4), filename or '<main script>', lineno)) \
                         for (i, (filename, lineno)) in enumerate(reversed(self.locations)))

    def copy(self, new_command = None, add_location = None):
        ''' Returns a copy of the line.
            If the new_command parameter is specified, that will be the command of the new line
            and it will get the same indentation as the old line. '''
        line = Line(self.command, self.locations, self.namespaces, calling_lines = self.calling_lines)

        if add_location:
            line.locations = line.locations + [add_location]

        if new_command:
            line.command = new_command

        return line

    def substitute_names(self, name_subst_dict):
        '''Return copy of line with a line.command substitution specified in name_subst_dict'''
        if not name_subst_dict:
            return self

        def repl_func(match):
            n = match.group(0)

            if n.endswith('.'):
                suffix = '.'
                n = n[:-1]
            else:
                suffix = ''

            if n in name_subst_dict:
                return name_subst_dict[n] + suffix
            else:
                return n + suffix

        s = varname_re.sub(repl_func, self.command)
        s = varname_dot_re.sub(repl_func, s)

        return self.copy(new_command = s)

    def replace_placeholders(self, placeholders = placeholders):
        replace_func = lambda matchobj: placeholders[int(matchobj.group(1))]
        self.command = re.sub(r'\{(\d+?)\}', replace_func, self.command)

    def __str__(self):
        return self.command

    def __repr__(self):
        return self.command

class Macro:
    '''Macro object used for handling macros before Ply lex/yacc parser'''
    def __init__(self, lines):
        self.lines = lines
        self.name, self.parameters = self.get_macro_name_and_parameters()

    def get_name_prefixed_by_namespace(self):
        return prefix_with_ns(self.name, self.lines[0].namespaces)

    def get_overloaded_name(self):
        return append_overloaded_name(self.name, self.parameters)

    def get_macro_name_and_parameters(self):
        '''Returns the function name, parameter list, and result variable (or None) as a tuple'''
        param = white_space_re + r'([$%@!?~]?[\w\.]+|#[\w\.]+#)' + white_space_re
        params = r'%s(,%s)*' % (param, param)
        m = re.match(r'(?ms)^\s*macro\s+(?P<name>[a-zA-Z0-9_]+(\.[a-zA-Z_0-9.]+)*)\s*(?P<params>\(%s\))?' % params, self.lines[0].command)

        if not m:
            raise ParseException(self.lines[0], "Syntax error in macro declaration!")

        name = m.group('name')
        params = m.group('params') or []

        if params:
            params = params[1:-1]                     # strip parenthesis
            params = re.sub(white_space, '', params)  # strip whitespace (e.g. comments)
            params = [x.strip() for x in params.split(',')]

        return (name, params)

    def copy(self, lines=None, add_location=None):
        if lines is None:
            lines = self.lines[:]

        return Macro([l.copy(add_location=add_location) for l in lines])

    def substitute_names(self, replace_string_placeholders, name_subst_dict):
        '''Returns a copy of the block with the specified name substitutions made'''
        new_macro = self.copy(lines = [line.substitute_names(name_subst_dict) for line in self.lines])

        if replace_string_placeholders:
            for line in new_macro.lines:
                line.replace_placeholders()

        # handle raw replacements (arguments like #var# should be substituted irrespectively of context)
        for name1, name2 in list(name_subst_dict.items()):
            if name1.startswith('#'):
                for line in new_macro.lines:
                    line.command = line.command.replace(name1, name2)

        return new_macro

def merge_lines(lines):
    '''Converts a list of Line objects to a source code string.  \n
       This will remove any context information such as locations or namespaces'''
    return '\n'.join([line.command for line in lines])

def parse_lines(s, basepath = None, filename = None, namespaces = None):
    '''converts a source code string to a list of Line objects'''
    def process_f_string(line):
        in_string = False
        in_f_string = False
        f_connect = False
        hyphen_connect = False
        escaping = False

        all_args = []
        arg_content = ''
        record_arg = False
        f_spots = []

        for i, c in enumerate(line):
            if record_arg == True:
                if c == '>' and not escaping and not hyphen_connect:
                    record_arg = False
                    escaping = False
                    all_args.append(arg_content)
                    arg_content = ''
                    continue
                else:
                    arg_content += c

            if c == 'f' and not in_string:
                f_connect = True
            elif in_string and c == '\\':
                escaping = True
            elif in_string and c == '-':
                hyphen_connect = True
            elif c == "'" and not escaping:
                in_string = not in_string
                in_f_string = f_connect and in_string

                if in_f_string:
                    f_spots.append(i - 1)
            elif in_f_string and not escaping:
                if c == '<':
                    record_arg = True

            if c != 'f':
                f_connect = False
            if c != '-':
                hyphen_connect = False
            if c != '\\':
                escaping = False

        new_line = line
        deleted = 0
        for s in f_spots:
            index = s - deleted
            new_line = new_line[:index] + new_line[index+1:]
            deleted += 1

        for a in all_args:
            new_line = new_line.replace("<{}>".format(a), "\' & {} & \'".format(a.replace('\\>', '>').replace('\\<', '<')))

        return new_line

    if namespaces is None:
        namespaces = []

    s = handlePython(s, basepath)

    lines = s.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    lines = [process_f_string(l) for l in lines]

    # encode lines numbers as '[[[lineno]]]' at the beginning of each line
    lines = ['[[[%.5d]]]%s' % (lineno+1, x) for (lineno, x) in enumerate(lines)]

    s = '\n'.join(lines)

    # remove comments and multi-line indicators ('...\n')
    s = comment_re.sub('', s)

    lines = s.split('\n')


    # NOTE(Sam): Remove any occurances of the new comment type //
    for i in range(len(lines)):
        m = re.search(r"^(?:(?!\/\/|[\"\']).|[\"\'][^\"\']*[\"\'])*(\/\/.*$)", lines[i])

        if m:
            lines[i] = lines[i].replace(m.group(1), "")

    s = '\n'.join(lines)
    s = line_continuation_re.sub('', s)

    # construct Line objects by extracting the line number and line parts
    lines = []

    for line in s.split('\n'):
        lineno, line = int(line[3:3 + 5]), line[3 + 5 + 3:]
        line = placeholder_re.sub('', line)
        lines.append(Line(line, [(filename, lineno)], namespaces))

    convert_strings_to_placeholders(lines)

    return collections.deque(lines)

def handlePython(code, basepath):
    # Use re.DOTALL to make '.' match newlines
    run_re = re.compile(r"run\s*<<(?P<code>.+?)>>", re.DOTALL)
    read_re = re.compile(r"read\s*<<(?P<code>.+?)>>", re.DOTALL)

    namespace = {'__builtins__': __builtins__}
    namespace = {'basepath': basepath}
    namespace.update(globals())

    import textwrap
    def trimmed(s: str) -> str:
        """
        Removes the minimum common indentation from all lines in a Python code string,
        so that the root-level hierarchy starts at the very beginning of the line.
        """
        return textwrap.dedent(s)

    # Change directory to compile path
    wd = os.getcwd()
    if basepath:
        os.chdir(basepath)

    new_code = code
    finished = False
    while (not finished):
        finished = True

        # Process all run<< >> blocks first
        for m in run_re.finditer(new_code):  # Process in reverse to maintain indices
            exec_code = trimmed(m.group('code'))
            exec(exec_code, namespace)
            new_code = new_code[:m.start()] + new_code[m.end():]

            finished = False
            break

        if not finished:
            continue

        # Then process all read<< >> blocks
        for m in read_re.finditer(new_code):  # Process in reverse to maintain indices
            eval_code = trimmed(m.group('code'))
            final = eval(eval_code, namespace)

            if isinstance(final, int):
                final = str(final)
            elif isinstance(final, list):
                final = '\n'.join(final)
            else:
                final = str(final)

            new_code = new_code[:m.start()] + final + new_code[m.end():]

            finished = False
            break

        if not finished:
            continue

    if basepath:
        os.chdir(wd)

    return new_code

def convert_strings_to_placeholders(lines):
    '''Converts all strings to placeholders, appending string to placeholder dictionary'''
    def replace_func(match):
        i = len(placeholders)

        # replace the match with a placeholder (eg. "{8}") and store the replaced string
        s = match.group(0)

        # convert single quotes (') to double quotes (")
        if s and s[0] == "'":
            s = '"%s"' % s[1:-1].replace(r"\'", "'")

        placeholders[i] = s

        return '{%d}' % i

    # substitute strings with placeholders
    if hasattr(lines, '__iter__'):
        for l in lines:
            l.command = string_re.sub(replace_func, l.command)
    else:
        lines.command = string_re.sub(replace_func, lines.command)

def parse_lines_and_handle_imports(basepath, source, compiler_import_cache, filename = None, namespaces = None, preprocessor_func = None):
    '''parses lines into Line objects and imports all files. preprocessor_func does not mean preprocessor_plugins'''

    def read_path(basepath, filepath):
        # import from URL
        if filepath.startswith('http://') or filepath.startswith('https://'):
            from urllib.request import urlopen

            s = urlopen(filepath, timeout = 5).read().decode('utf-8')
            src = re.sub('\r+\n*', '\n', s)

            return [(filepath, src)]

        path = os.path.abspath(os.path.join(basepath, filepath))

        # list of paths to import (covers the case of importing a folder)
        paths = []

        if os.path.exists(path):
            # see if we're importing a folder or a file
            if os.path.isdir(path):
                for root, dirs, files in os.walk(path):
                    for f in files:
                        split = os.path.splitext(f)

                        if split[1] == '.ksp':
                            paths.append(os.path.join(root, f))
            elif os.path.isfile(path):
                paths.append(path)
        else:
            raise ParseException(line, 'Imported path does not exist or could not be read!\n\n'
                                       'Try saving .ksp files before compiling in order to make relative paths work, '
                                       'or check existence of the following path:\n\n%s' % path)

        out_data = []

        # actually open everything in paths list sequentially
        for p in paths:
            with io.open(p, 'r', encoding = 'utf-8') as s:
                src = '\n' + re.sub('\r+\n*', '\n', s.read())

                out_data.append((p, src))

        return out_data

    if preprocessor_func:
        source = preprocessor_func(source, namespaces)

    lines = parse_lines(source, basepath, filename, namespaces)
    new_lines = collections.deque()

    while lines:
        line = lines.popleft()

        # if line seems to be an import line
        if import_ignore_re.match(line.command):
            new_lines.clear()
            return new_lines
        elif import_basic_re.match(line.command):
            line.replace_placeholders()

            # check if it matches a more elaborate syntax
            m = import_re.match(str(line))

            if not m:
                raise ParseException(line, 'Syntax error in import statement!')

            # load the code in the given file
            filename = m.group('filename')
            namespace = m.group('asname')

            new_sources = read_path(basepath, filename)

            for path, source in new_sources:
                if path not in compiler_import_cache:
                    compiler_import_cache.append(path)

                    # parse code and add an extra namespace if applicable
                    namespaces = line.namespaces

                    if namespace:
                        namespaces = namespaces + [namespace]

                    preproc_s = source

                    if preprocessor_func:
                        preproc_s = preprocessor_func(source, namespaces)

                    new_lines.extend(parse_lines_and_handle_imports(basepath, preproc_s, compiler_import_cache, path, namespaces))
        # non-import line so just add it to result line list:
        else:
            new_lines.append(line)

    return new_lines

def handle_conditional_lines(lines):
    '''handle SET_CONDITION, RESET_CONDITION, USE_CODE_IF and USE_CODE_IF_NOT'''
    use_code_conds = []
    false_index = -1

    for line_obj in lines:
        line = line_obj.command
        ls_line = line.lstrip()

        clear_this_line = false_index + 1

        if 'END_USE_CODE' in line:
            if use_code_conds.pop() == False and len(use_code_conds) == false_index:
                false_index = -1

            clear_this_line = True

        if not clear_this_line and 'SET_CONDITION(' in line:
            m = re.search('\\((.+?)\\)', line)

            if m:
                cond = m.group(1).strip()

                if line.lstrip().startswith('SET_CONDITION('):
                    true_conditions.add(cond)

                    if not cond.startswith('NO_SYS'):  # if it starts with NO_SYS, then leave it in the code
                        clear_this_line = True
                elif line.lstrip().startswith('RESET_CONDITION('):
                    if cond in true_conditions:
                        true_conditions.remove(cond)

                    if not cond.startswith('NO_SYS'):  # if it starts with NO_SYS, then leave it in the code
                        clear_this_line = True

        if 'USE_CODE_IF' in line:
            m = re.search('\\((.+?)\\)', line)

            if m:
                cond = m.group(1).strip()

                if line.lstrip().startswith('USE_CODE_IF('):
                    if false_index == -1 and cond not in true_conditions:
                        false_index = len(use_code_conds)

                    use_code_conds.append(cond in true_conditions)

                    clear_this_line = True
                elif line.lstrip().startswith('USE_CODE_IF_NOT('):
                    if false_index == -1 and cond in true_conditions:
                        false_index = len(use_code_conds)

                    use_code_conds.append(cond not in true_conditions)

                    clear_this_line = True

        if clear_this_line:
            line_obj.command = re.sub(r'[^\r\n]', '', line)

def extract_macros(lines_deque):
    '''returns (cleaned_lines, macros)'''
    macros = []
    lines = lines_deque
    cleaned_lines = []

    while lines:
        line = lines.popleft()

        # if macro definition found, read lines up until the next "end macro"
        if macro_start_re.match(line.command):
            found_end = False
            macro_lines = [line]

            while lines:
                line = lines.popleft()
                macro_lines.append(line)

                if macro_end_re.match(line.command):
                    found_end = True
                    break

                if macro_start_re.match(line.command):
                    raise ParseException(line, "Macro definitions cannot be nested! Maybe you forgot an 'end macro' line earlier?")

            if not found_end:
                raise ParseException(macro_lines[0], "Could not find a corresponding 'end macro' statement.")

            macros.append(Macro(macro_lines))
        # else if line outside of macro definition
        else:
            cleaned_lines.append(line)

    return (cleaned_lines, macros)

def extract_callback_lines(lines):
    '''returns (normal_lines, callback_lines)'''
    normal_lines = []
    callback_lines = []
    inside_callback = False

    for line in lines:
        if re.match(r'\s*on\s+(ui_control(s\s*$)?)', line.command):
            inside_callback = True
            callback_lines.append(line)
        elif re.match(r'\s*end on\b', line.command):
            inside_callback = False
            callback_lines.append(line)
        else:
            # are we currently inside a callback or not?
            if inside_callback:
                callback_lines.append(line)
            else:
                normal_lines.append(line)

    return (normal_lines, callback_lines)

def sub_defines(lines, cur_line, define_cache):
    from preprocessor_plugins import macro_iter_functions, post_macro_iter_functions, substituteDefines

    convert_strings_to_placeholders(lines)
    substituteDefines(lines, define_cache)

    while macro_iter_functions(lines, placeholders):
        convert_strings_to_placeholders(lines)
        substituteDefines(lines, define_cache)

    while post_macro_iter_functions(lines, placeholders):
        convert_strings_to_placeholders(lines)
        substituteDefines(lines, define_cache)

    for c in lines:
        if not cur_line.calling_lines:
            c.calling_lines = [cur_line]
        else:
            c.calling_lines = cur_line.calling_lines + [cur_line]

def expand_macros(lines, macros, level = 0, replace_raw = True, define_cache = None):
    '''Inline macro invocations by the body of the macro definition (with parameters properly replaced)
        returns tuple (normal_lines, callback_lines) where the latter are callbacks'''
    macro_call_re = re.compile(r'(?ms)^\s*([\w_.]+)\s*(\(.*\))?%s$' % white_space_re)
    name2macro = {}

    for m in macros:
        m.name = m.get_name_prefixed_by_namespace()
        name = m.get_overloaded_name()

        if not (name == 'tcm.init' and name in name2macro):
            name2macro[name] = m

    orig_lines = lines
    lines = collections.deque(orig_lines)
    new_lines = []
    new_callback_lines = []
    num_substitutions = 0

    while lines:
        line = lines.popleft()
        new_lines.append(line)
        m = macro_call_re.match(line.command)

        if m:
            macro_name, args = m.group(1), m.group(2)
            macro_name = prefix_with_ns(macro_name, line.namespaces)

            if args:
                macro_name = append_overloaded_name(macro_name, utils.split_args(args[1:-1], line))
            else:
                macro_name = append_overloaded_name(macro_name, [])

            if macro_name in name2macro:
                new_lines.pop()

                macro = name2macro[macro_name]

                if args:
                    args = utils.split_args(args[1:-1], line)
                else:
                    args = []

                # verify that the parameter count is correct
                if len(macro.parameters) != len(args):
                    raise ParseException(line, "Wrong number of parameters for %s()! Expected %d, got %d." % (macro_name, len(macro.parameters), len(args)))

                if level > 40:
                    raise ParseException(line, "This macro seems to be invoking itself recursively, which is not allowed!")

                # build a substitution mapping parameters to arguments, and substitute
                name_subst_dict = dict(list(zip(macro.parameters, args)))

                macro = macro.copy(add_location = line.locations[0])
                macro = macro.substitute_names(replace_raw, name_subst_dict)

                # add macro body
                if args:
                    macro_call_str = '%s(%s)' % (macro_name, ', '.join([re.sub(white_space, '', a).strip() for a in args]))
                else:
                    macro_call_str = '%s' % (macro_name)

                # erase any inner comments to not disturb outer
                macro_call_str = re.sub(white_space, '', macro_call_str)
                normal_lines, callback_lines = extract_callback_lines(macro.lines[1:-1])

                sub_defines(normal_lines, line, define_cache)
                sub_defines(callback_lines, line, define_cache)

                new_lines.extend(normal_lines)
                new_callback_lines.extend(callback_lines)

                num_substitutions += 1

    if num_substitutions:
        return expand_macros(new_lines + new_callback_lines, macros, level + 1, replace_raw, define_cache)
    else:
        return (new_lines, new_callback_lines)

class ASTModifierBase(ksp_ast_processing.ASTModifier):
    '''Class for accessing AST nodes for modification'''
    def __init__(self, modify_expressions = False):
        ksp_ast_processing.ASTModifier.__init__(self, modify_expressions = modify_expressions)

    def modifyFunctionCall(self, node, *args, **kwargs):
        '''there are some functions/preprocessor directives for which the first parameter should always be left as is'''
        if node.function_name.identifier in ['SET_CONDITION', 'RESET_CONDITION', 'USE_CODE_IF', 'USE_CODE_IF_NOT',
                                             '_pgs_create_key', '_pgs_key_exists', '_pgs_set_key_val', '_pgs_get_key_val',
                                             'pgs_create_key', 'pgs_key_exists', 'pgs_set_key_val', 'pgs_get_key_val',
                                             'pgs_create_str_key', 'pgs_str_key_exists', 'pgs_set_str_key_val', 'pgs_get_str_key_val']:
            first_parameter_to_change = 1
        else:
            first_parameter_to_change = 0

        node.function_name = self.modify(node.function_name, *args, **kwargs)
        node.parameters[first_parameter_to_change:] = [self.modify(p, *args, **kwargs)
                                                       for p in node.parameters[first_parameter_to_change:]]

        if node.is_procedure:
            return [node]
        else:
            return node

class ASTModifierCombineCallbacks(ASTModifierBase):
    '''Combines callbacks by generating a dictionary of used CBs and extending existing ones with lines in the duplicate CB'''
    def __init__(self, ast, combine_callbacks):
        self.combine_callbacks = combine_callbacks
        ASTModifierBase.__init__(self, modify_expressions = True)
        self.traverse(ast, parent_function = None, function_params = [], parent_families = [])

    def modifyModule(self, node, *args, **kwargs):
        callbacks = {}

        for b in node.blocks:
            if isinstance(b,ksp_ast.Callback):
                ui_name = ""

                if b.variable:  # ui_controls have a variable which is stored as a child component
                    if b.lexinfo[3]: # If the ui_control has been imported with a namespace
                        ui_name = "".join(b.lexinfo[2])

                    ui_name += str(b.variable)

                cb_key = b.name + ui_name

                if cb_key in callbacks:
                    if  self.combine_callbacks:
                        children = b.get_childnodes()

                        if b.variable:
                            children = children[1:] # Removes ui_name from children to prevent duplicate CBs

                        callbacks[cb_key].lines.extend(children) # Extend the existing CB with lines from the duplicate
                        b.lines = [] # Delete lines of duplicate CBs
                else:
                    callbacks[cb_key] = b

        if self.combine_callbacks:
            # Removes the CBs with no lines from node.blocks
            node.blocks = [b for b in node.blocks if (isinstance(b, ksp_ast.Callback) and b.lines != []) or not isinstance(b, ksp_ast.Callback)]

class ASTModifierNodesToNativeKSP(ASTModifierBase):
    '''Travel through AST and modify nodes to native KSP'''
    def __init__(self, ast, line_map):
        ASTModifierBase.__init__(self, modify_expressions = True)
        self.line_map = line_map
        self.traverse(ast, parent_function = None, function_params = [], parent_families = [])

    def modifyModule(self, node, *args, **kwargs):
        '''find and extract 'on init' block'''
        on_init_block = None

        for b in node.blocks:
            if isinstance(b, ksp_ast.Callback) and b.name == 'init':
                node.blocks.remove(b)
                on_init_block = b
                break

        # if there was none, create one
        if on_init_block is None:
            on_init_block = ksp_ast.Callback(node.lexinfo, 'init', lines = [])

        node.on_init = on_init_block

        # insert it as the first block
        node.blocks.insert(0, on_init_block)

        ASTModifierBase.modifyModule(self, node, *args, **kwargs)

        # in case some function definition has been overriden, keep only the version among functions.value()
        node.blocks = [b for b in node.blocks if not (isinstance(b, ksp_ast.FunctionDef) and functions[b.name.identifier] != b)]

        return node

    def modifyForStmt(self, node, *args, **kwargs):
        '''Convert for-loops into while loops'''

        if node.downto:
            op = '>='
            incdec = 'dec'
        else:
            op = '<='
            incdec = 'inc'

        # optimize "for x := 0 to N-1" into "while x < N" instead of the normal "while x <= N-1"
        if not node.downto and isinstance(node.end, ksp_ast.BinOp) and node.end.op == '-' and isinstance(node.end.right, ksp_ast.Integer) and node.end.right.value == 1:
            op = '<'
            node.end = node.end.left  # skip the -1 part (keep only the left operand)

        loop_condition = ksp_ast.BinOp(node.lexinfo, node.loopvar.copy(), op, node.end)

        # if a step is specified then the increment is formulated as x := x + step
        if node.step:
            incdec_op = {'inc': '+',
                         'dec': '-'}[incdec]
            incdec_statement = ksp_ast.AssignStmt(node.lexinfo, node.loopvar.copy(), ksp_ast.BinOp(node.lexinfo, node.loopvar.copy(), incdec_op, node.step))
        # otherwise we use inc(x) or dec(x)
        else:
            incdec_statement = ksp_ast.FunctionCall(node.lexinfo, ksp_ast.ID(node.lexinfo, incdec), [node.loopvar.copy()], is_procedure = True)

        statements = [ksp_ast.AssignStmt(node.lexinfo, node.loopvar.copy(), node.start),
                      ksp_ast.WhileStmt(node.lexinfo, loop_condition,
                                        node.statements + [incdec_statement])]
        return flatten([self.modify(stmt, *args, **kwargs) for stmt in statements])

    def modifyIfStmt(self, node, *args, **kwargs):
        '''Convert if > else if > else statements into just if-else statements by nesting them inside each other'''

        # modify the if condition and the statements in the if-body
        if_condition, stmts = node.condition_stmts_tuples[0]
        if_condition, stmts = (self.modify(if_condition, *args, **kwargs),
                               flatten([self.modify(s, *args, **kwargs) for s in stmts]))
        node.condition_stmts_tuples[0] = (if_condition, stmts)

        # if if-else statement (i.e. no else-if part)
        if len(node.condition_stmts_tuples) == 2 and node.condition_stmts_tuples[1][0] is None:
            stmts = node.condition_stmts_tuples[1][1]
            stmts = flatten([self.modify(s, *args, **kwargs) for s in stmts])
            node.condition_stmts_tuples[1] = (None, stmts)

        # else if there is any "else if" part
        elif len(node.condition_stmts_tuples) > 1 and node.condition_stmts_tuples[1][0] is not None:
            else_if_condition, else_if_stmts = node.condition_stmts_tuples[1]
            # create a new if-statement consisting of the that 'else if' and all the following 'else if'/'else' clauses, and modify this recursively
            new_if_stmt = self.modifyIfStmt(ksp_ast.IfStmt(node.condition_stmts_tuples[1][0].lexinfo, node.condition_stmts_tuples[1:]), *args, **kwargs)[0]
            # the new contents of the else part will be the if-statement just created
            node.condition_stmts_tuples = [node.condition_stmts_tuples[0],
                                           (None, [new_if_stmt])]

        return [node]

    def modifyPropertyDef(self, node, *args, **kwargs):
        '''Check properties, prefix names and modify node as a getter or setter object'''

        # check syntax
        for func_name in list(node.functiondefs.keys()):
            if func_name not in ['get', 'set']:
                raise ksp_ast.ParseException(node.functiondefs[func_name], "Expected function with name 'get' or 'set', but found '%s'!" % func_name)

        if not node.get_func_def and not node.set_func_def:
            raise ksp_ast.ParseException(node, "Expected function with name 'get' or 'set', but found no function definition!")

        if node.get_func_def and not node.get_func_def.return_value:
            raise ksp_ast.ParseException(node.get_func_def, "The 'get' function needs to have a return value!")

        if node.set_func_def and node.set_func_def.return_value:
            raise ksp_ast.ParseException(node.set_func_def, "The 'set' function cannot have a return value!")

        if node.set_func_def and not node.set_func_def.parameters:
            raise ksp_ast.ParseException(node.set_func_def, "The 'set' function must have at least one parameter!")

        # prefix the name
        if kwargs['parent_families']:
            node.name = prefix_ID_with_ns(node.name, kwargs['parent_families'], kwargs['function_params'], force_prefixing = True)
        else:
            node.name = self.modifyID(node.name, kwargs['parent_function'], kwargs['parent_families'])

        # change the get/set function names before proceeding to them
        if node.get_func_def:
            node.get_func_def.name.identifier = node.name.identifier + '.get'   # if property is called prop, then this function is named prop.get
            node.get_func_def = self.modify(node.get_func_def, parent_function = None, function_params = [], parent_families = [], add_name_prefix = False)

        if node.set_func_def:
            node.set_func_def.name.identifier = node.name.identifier + '.set'   # if property is called prop, then this function is named prop.set
            node.set_func_def = self.modify(node.set_func_def, parent_function = None, function_params = [], parent_families = [], add_name_prefix = False)

        # add property to list
        properties.add(node.name.identifier)

        return []

    def modifyFunctionDef(self, node, parent_function = None, function_params = None, parent_families = None, add_name_prefix = True):
        ''' Pass along some context information (e.g. what function definition we're inside and which parameters it has)
            Also initialize some member variables on the function object that will be modified inside its body (e.g. local variable declarations) '''

        node.local_declaration_statements = []
        node.global_declaration_statements = []
        node.taskfunc_declaration_statements = []
        node.locals_name_subst_dict = {}  # maps local variable names to their global counterpart
        node.locals = set()               # a set containing the keys in node.locals_name_subst_dict, but all in lowercase
        node.used = False

        # as we visit the function definition, pass along information to nodes further down the tree of the function parameter names
        params = function_params + node.parameters

        # verify that there are no duplicate argument names in the function definition
        if len(set(params)) < len(params):
            raise ksp_ast.ParseException(node, "Function contains duplicate argument names!")

        if node.return_value:
            params.append(node.return_value.identifier)

        # allows def prefix_with_ns to compare current functions against builtins before prefixing namespaces
        if not node.name.identifier in functions_before_prefix:
            functions_before_prefix[node.name.identifier] = node

        # modify name first (add namespace prefix)
        if add_name_prefix:
            node.name = self.modify(node.name, parent_function = node, function_params = params, parent_families = parent_families)

        # add function to table of available functions
        if node.name.identifier in functions and not node.override:
            # if this function is overriden
            if functions[node.name.identifier].override:
                node.lines = []  # clear the lines so we don't accidentally introduce some performance cost by handling these later
            else:
                raise ksp_ast.ParseException(node, 'Function already declared!')
        else:
            functions[node.name.identifier] = node

        # modify the body of the function
        node.lines = flatten([self.modify(l, parent_function = node, function_params = params, parent_families = parent_families) for l in node.lines])

        return node

    def modifyFamilyStmt(self, node, parent_function = None, function_params = None, parent_families = None):
        '''First make sure the name of the family is prefixed with the right namespace, then pass this name as context to the further handling of the family body'''

        if parent_families is None:
            parent_families = []

        # only add namespaces to the outermost family name if multiple ones are nested
        if not parent_families:
            node.name = self.modify(node.name, parent_function = parent_function, function_params = function_params, parent_families = parent_families)

        # add family name to the table of all used families
        global_family_name = '.'.join(parent_families + [node.name.identifier])
        families.add(global_family_name)

        # then modify statements and pass along information to nodes further down the tree of the chain of family definitions so far
        node.statements = flatten([self.modify(n, parent_function = parent_function,
                                               function_params = function_params,
                                               parent_families = parent_families + [str(node.name)]) for n in node.statements])
        return [node.statements]

    def add_global_var(self, global_varname, modifiers):
        '''Add global variables to variable set'''
        is_ui_declaration = any([m for m in modifiers if m.startswith('ui_')])

        # add variable to list of variables
        variables.add(global_varname.lower())

        if is_ui_declaration:
            ui_variables.add(global_varname.lower())

    def handleLocalDeclaration(self, node, func):
        ''' Handle variable declaration made inside the function node given as parameter.
            If the global modifier is not used, create a globally unique name for the local variable and add
            info to the name substitution table (locals_name_subst_dict) of the function on how to translate
            occurances of the local name to the globally unique one.
            Add the globally unique version of the variable to the variables list and extract the declaration line
            so that it can later be moved to the init callback '''

        # result is by default empty since declaration line will be moved to 'on init'
        result = []

        # if a global variable declaration made inside a function
        is_local = 'local' in node.modifiers
        is_global = ('on_init' in func.name.identifier.lower() and not 'local' in node.modifiers) or ('global' in node.modifiers)

        if is_global:
            global_varname = node.variable.prefix + node.variable.identifier
            self.add_global_var(global_varname, node.modifiers)

            if 'global' in node.modifiers:
                node.modifiers.remove('global')

        # if local variable declaration
        else:
            # if there is an initial value, then add an assignment statement for this (eg. inside a function "declare x := 5" is replaced by "x := 5")
            if node.initial_value and not 'const' in node.modifiers and not node.size:
                var = node.variable.copy()

                # copy also extra attribute added by this class, to make sure that we don't add namespace prefix a second time
                if hasattr(node.variable, 'namespace_prefix_done'):
                    var.namespace_prefix_done = node.variable.namespace_prefix_done

                result.append(ksp_ast.AssignStmt(node.lexinfo, ksp_ast.VarRef(node.lexinfo, var), node.initial_value))

            # check that the local variable name has not previously been used and add it to the list of locals
            local_varname = node.variable.identifier

            if local_varname.lower() in func.locals:
                raise ksp_ast.ParseException(node.variable, "Local variable {} redeclared!".format(local_varname))

            func.locals.add(local_varname.lower())

            # add info about how to map the local name to the global name so that occurances of the local name can later be modified
            if func.is_taskfunc and not 'local' in node.modifiers:
                # e.g. map x to %p[$sp + 1], where 1 is used for the first local declaration, 2 for the second and so on
                var_index = len(func.taskfunc_declaration_statements) + 1
                li = node.variable.lexinfo
                func.locals_name_subst_dict[local_varname] = ksp_ast.VarRef(node.lexinfo, ksp_ast.ID(node.lexinfo, '%p'),
                                                                            [ksp_ast.BinOp(li, ksp_ast.VarRef(li, ksp_ast.ID(li, '$fp')), '+', ksp_ast.Integer(li, var_index))])
            else:
                # e.g. map x to $_x

                # change name of variable to be a combination of the function name and the declared name (and make sure it's unique)
                global_varname = '%s_%s' % (node.variable.prefix, local_varname)
                i = 2

                while global_varname.lower() in variables:
                    global_varname = '%s_%s%d' % (node.variable.prefix, local_varname, i)
                    i += 1

                func.locals_name_subst_dict[local_varname] = ksp_ast.VarRef(node.lexinfo, ksp_ast.ID(node.lexinfo, global_varname))

                node.variable.prefix, node.variable.identifier = global_varname[0], global_varname[1:]

                if 'local' in node.modifiers:
                    node.modifiers.remove('local')

                self.add_global_var(global_varname, node.modifiers)

        if is_global:
            func.global_declaration_statements.append(node)
        elif func.is_taskfunc and not is_local:
            func.taskfunc_declaration_statements.append(node)
        else:
            func.local_declaration_statements.append(node)

        return result

    def modifyDeclareStmt(self, node, *args, **kwargs):
        ''' Add a variable prefix ($ or %) to the declaration, if not already given.
            If declared inside a family then prefix it with the names of the family definitions we're currently inside.
            Otherwise prefix the name with the namespaces of the line it's declared on (when that line was imported using "import ... as").
            Handle the case when the variable is declared inside a user-defined function. '''

        # default handling of everything except the variable name:
        node.size = self.modify(node.size, *args, **kwargs)

        if type(node.initial_value) is list:
            node.initial_value = [self.modify(v, *args, **kwargs) for v in node.initial_value]
        elif node.initial_value:
            node.initial_value = self.modify(node.initial_value, *args, **kwargs)

        node.parameters = [self.modify(p, *args, **kwargs) for p in node.parameters]

        # if variable was declared inside a family, prefix it with the namespaces given by the chain of nested families it's declared inside
        if kwargs['parent_families']:
            node.variable = prefix_ID_with_ns(node.variable, kwargs['parent_families'], kwargs['function_params'], force_prefixing = True)
        # otherwise treat it as any other name
        else:
            node.variable = self.modifyID(node.variable, kwargs['parent_function'], kwargs['parent_families'], is_name_in_declaration = True)

        # if no variable prefix used, add one automatically
        if not node.variable.prefix:
            from decimal import Decimal

            if node.size is not None:
                if node.isUIDeclaration() and 'ui_xy' in node.modifiers:
                    node.variable.prefix = '?'
                else:
                    if node.initial_value is not None:
                        try:
                            expr_eval = comp_extras.evaluate_expression(node.initial_value[0])

                            if isinstance(expr_eval, str):
                                node.variable.prefix = '!'
                            elif isinstance(expr_eval, Decimal):
                                node.variable.prefix = '?'
                            else:
                                node.variable.prefix = '%'
                        except:
                            node.variable.prefix = '%'
                    else:
                        node.variable.prefix = '%'

            else:
                if 'const' not in node.modifiers:
                    expr_eval = comp_extras.evaluate_expression(node.initial_value)

                    # this won't work because of handleSameLineDeclaration() in the preprocessor, alas
                    #if isinstance(expr_eval, str):
                    #   node.variable.prefix = '@'
                    if isinstance(expr_eval, Decimal):
                        node.variable.prefix = '~'
                    else:
                        node.variable.prefix = '$'
                else:
                    node.variable.prefix = '$'

        # is this declaration made inside of a function?
        if kwargs['parent_function']:
            lines = self.handleLocalDeclaration(node, kwargs['parent_function'])
            lines = flatten([self.modify(n, *args, **kwargs) for n in lines])

            return lines
        else:
            vname = node.variable.prefix + node.variable.identifier.lower()
            self.add_global_var(vname, node.modifiers)

            return [node]

    def modifyVarRef(self, node, parent_function = None, function_params = None, parent_families = None, is_name_in_declaration = False):
        '''Translate any references to local variables to their real name
           Note 1: previously this could all be handled in modifyID, but since the taskfunc system introduced VarRef objects with subscripts
                   we now need to handle it here since if you replace eg. x by %p[$sp + 1] the subscript need to be included in the resulting node.
           Note 2: not all identifiers have a parent VarRef so some replacements are also taken care of in the modifyID routine (see below).'''

        if parent_function and node.identifier.identifier in parent_function.locals_name_subst_dict:
            new_node = parent_function.locals_name_subst_dict[node.identifier.identifier]

            if node.subscripts and isinstance(new_node, ksp_ast.VarRef):  # combine subscripts
                subscripts = [self.modify(s, parent_function, function_params, parent_families, is_name_in_declaration)
                              for s in node.subscripts + new_node.subscripts]
                new_node = ksp_ast.VarRef(new_node.lexinfo, new_node.identifier, subscripts)

            return new_node

        return super(ASTModifierNodesToNativeKSP, self).modifyVarRef(node,                 \
                                                                     parent_function,      \
                                                                     function_params,      \
                                                                     parent_families,      \
                                                                     is_name_in_declaration)

    def modifyID(self, node, parent_function = None, function_params = None, parent_families = None, is_name_in_declaration = False):
        '''Add namespace prefix and translate references to local variables to their globally unique counterpart as determined by the translation table of the function.\n
           Look up the line object from the first macro preprocessor phase in order to extract information about the namespace'''

        namespaces = self.line_map[node.lineno].namespaces

        # make sure to not add namespace twice
        if hasattr(node, 'namespace_prefix_done'):
            id = node
        else:
            id = prefix_ID_with_ns(node, namespaces, function_params, force_prefixing = is_name_in_declaration)

        # if this is a local variable name, then replace it with the corresponding global name
        # most of these are handled by modifyVarRef, but here we catch some other cases like for example declaration statements (where the identifier isn't a VarRef)
        if parent_function and id.identifier in parent_function.locals_name_subst_dict:
            id = parent_function.locals_name_subst_dict[id.identifier].identifier

        # mark node to avoid that we add namespace twice
        id.namespace_prefix_done = True

        return id

class ASTModifierFixPrefixes(ASTModifierBase):
    '''Traverse AST and add prefixs to variables'''

    def __init__(self, ast):
        ASTModifierBase.__init__(self, modify_expressions = True)
        self.traverse(ast)

    def modifyFunctionDef(self, node, parent_function = None, parent_varref = None):
        return ASTModifierBase.modifyFunctionDef(self, node, parent_function = node) # pass along a reference to what function we're currently inside

    def modifyVarRef(self, node, parent_function = None, parent_varref = None):
        return ASTModifierBase.modifyVarRef(self, node, parent_function = parent_function, parent_varref = node) # pass along a reference to what varref we're currently inside

    def modifyID(self, node, parent_function = None, parent_varref = None):
        '''Add a variable prefix (one of $, %, @, !, ? and ~) to each variable based on the list of variables previously built'''
        name = node.prefix + node.identifier
        first_part = name.split('.')[0]

        # if prefix is missing and this is not a function or family and does not start with a function parameter
        # (e.g. if a parameter is passed as param and then referenced as param__member)
        if node.prefix == '' and not (name in functions or
                                      name in ksp_builtins.functions or
                                      name in families or
                                      name in properties or
                                      (parent_function and (first_part in parent_function.parameters or
                                                            parent_function.return_value and first_part == parent_function.return_value.identifier))):
            possible_prefixes = [prefix for prefix in variable_prefixes
                                 if prefix + name.lower() in variables or prefix + name in ksp_builtins.all_builtins]

            # if there is a subscript then only array types are possible
            if parent_varref and parent_varref.subscripts:
                possible_prefixes = [p for p in possible_prefixes if p not in '$@~']

            if len(possible_prefixes) == 0:
                raise ksp_ast.ParseException(node, "%s has not been declared!" % name)

            if len(possible_prefixes) > 1:
                raise ksp_ast.ParseException(node, "Type of %s is ambigious! Variable prefix could be any of the following: %s)" % (name, ', '.join(possible_prefixes)))

            node.prefix = possible_prefixes[0]

            return node

        elif node.prefix and not (name.lower() in variables or name in ksp_builtins.all_builtins):
            raise ksp_ast.ParseException(node, "%s has not been declared!" % name)
        else:
            return node

class ASTModifierFixPrefixesIncludingLocalVars(ASTModifierFixPrefixes):
    '''Assign variables with local/global modifiers a prefix'''

    def __init__(self, ast):
        ASTModifierFixPrefixes.__init__(self, ast)

    def modifyFunctionDef(self, node, parent_function = None):
        # pass along a reference to what function we're currently inside
        node = ASTModifierBase.modifyFunctionDef(self, node, parent_function = node)

        # extracted local/global variable declarations won't be handled automatically, so modify them explicitly here
        node.local_declaration_statements  = flatten([self.modify(n, parent_function = node) for n in node.local_declaration_statements])
        node.global_declaration_statements = flatten([self.modify(n, parent_function = node) for n in node.global_declaration_statements])

        # TODO: check if we need to handle this one too:
        #node.taskfunc_declaration_statements = flatten([self.modify(n, parent_function=node) for n in node.taskfunc_declaration_statements])

        return node

class ASTModifierIDSubstituter(ASTModifierBase):
    '''Class for replacing whole names with new names, eg. replace all "$x" by "$y" and all "$loop_counter" by "$plsd" (in case variable names are compacted).
       If there is an "x" to "y" substitution and it sees the name x.z, then it will NOT be translated into y.z. Only whole names are changed. '''

    def __init__(self, name_subst_dict, force_lower_case = False):
        self.name_subst_dict = name_subst_dict
        self.force_lower_case = force_lower_case
        ASTModifierBase.__init__(self, modify_expressions = True)

    def modifyID(self, node, *args, **kwargs):
        # Translate identifiers according to the translation table

        lookup_key = node.prefix + node.identifier

        if self.force_lower_case:
            lookup_key = lookup_key.lower()

        if lookup_key in self.name_subst_dict:
            new_identifier = self.name_subst_dict[lookup_key]

            if type(new_identifier) in (str, str):
                return ksp_ast.ID(node.lexinfo, new_identifier)
            else:
                raise ksp_ast.ParseException(node, "Internal error when replacing variables! Expected a string to replace with.")
        else:
            return node

class ASTModifierVarRefSubstituter(ASTModifierBase):
    '''Class for replacing certain variables references with an expression, eg. replace all "param" by "$y+1".
       If there is an "x" to "y" substitution and it sees the name x.z, then this will be translated into y.z.
       (unlike if ASTModifierIDSubstituter had been used) '''

    def __init__(self, name_subst_dict, inlining_function_node = None, subscript_addition = False):
        self.name_subst_dict = name_subst_dict
        self.inlining_function_node = inlining_function_node
        self.subscript_addition = subscript_addition
        ASTModifierBase.__init__(self, modify_expressions = True)

    def modify(self, node, *args, **kwargs):
        '''if a reference to a parent function that is being inlined should be added, then add it to the result (each statement of the result in case it's a list)'''
        result = ASTModifierBase.modify(self, node, *args, **kwargs)
        if not (self.inlining_function_node is None):
            if result is None:
                pass
            elif type(result) is list:
                for stmt in result:
                    stmt.lexinfo[2].append(self.inlining_function_node)
            else:
                result.lexinfo[2].append(self.inlining_function_node)

        return result

    def modifyVarRef(self, node, *args, **kwargs):
        '''Added relevant identifier and subscripts to variables'''
        if node.identifier.identifier_first_part in self.name_subst_dict:
            new_expr = self.name_subst_dict[node.identifier.identifier_first_part]

            if isinstance(new_expr, ksp_ast.VarRef):
                if self.subscript_addition:
                    # figure out which subscript to use or add if there are two
                    if not new_expr.subscripts and not node.subscripts:
                        subscripts = []
                    elif len(new_expr.subscripts) == 1 and not node.subscripts:
                        subscripts = [self.modify(s, *args, **kwargs) for s in new_expr.subscripts]
                    elif not new_expr.subscripts and len(node.subscript) == 1:
                        subscripts = [self.modify(s, *args, **kwargs) for s in node.subscripts]
                    elif len(new_expr.subscripts) == 1 and len(node.subscripts) == 1:
                        subscripts = [ksp_ast.BinOp(node.lexinfo, new_expr.subscripts[0], '+', node.subscripts[0])]
                else:
                    subscripts = [self.modify(s, *args, **kwargs) for s in node.subscripts] + new_expr.subscripts

                # build a new VarRef where the first part of the identifier has been replaced by the new_expr name.
                return ksp_ast.VarRef(new_expr.lexinfo,
                                      ksp_ast.ID(new_expr.identifier.lexinfo, new_expr.identifier.prefix + new_expr.identifier.identifier + node.identifier.identifier_last_part),
                                      subscripts = subscripts)
            else:
                return new_expr
        else:
            return ASTModifierBase.modifyVarRef(self, node, *args, **kwargs)

    def modifyFunctionDef(self, node, *args, **kwargs):
        '''don't modify the function name ID (replacing it with an expression is not the right thing to do...)
           modify everything else (the lines in the body)'''
        node.lines = flatten([self.modify(l, *args, **kwargs) for l in node.lines])

        return node

    def modifyFunctionCall(self, node, *args, **kwargs):
        '''Check if the function name in a function call is a parameter and substitute its name if that's the case'''
        func_call = node

        if node.function_name.identifier_first_part in self.name_subst_dict:
            # fetch the replacement expression and ensure that it's a variable reference
            new_expr = self.name_subst_dict[node.function_name.identifier_first_part]

            if not isinstance(new_expr, ksp_ast.VarRef):
                raise ksp_ast.ParseException(node, 'Expected a function name parameter!')

            # create a new FunctionCall object with the substituted function name
            function_name = ksp_ast.ID(new_expr.identifier.lexinfo, new_expr.identifier.identifier + node.function_name.identifier_last_part)
            func_call = ksp_ast.FunctionCall(new_expr.lexinfo, function_name, node.parameters, node.is_procedure, node.using_call_keyword)

        return ASTModifierBase.modifyFunctionCall(self, func_call, *args, **kwargs)

    def modifyID(self, node, *args, **kwargs):
        '''Translate identifiers according to the translation table (used for inlining functions, see ASTModifierFunctionExpander)'''
        if node.identifier_first_part in self.name_subst_dict:
            raise Exception('Although we expected this not to be the case, we have ID = %s, %s!' % (node.identifier, self.name_subst_dict))

            new_expr = self.name_subst_dict[node.identifier]

            if type(new_expr) in (str, str):
                new_expr = ksp_ast.ID(node.lexinfo, new_expr)
            else:
                raise Exception('Error!')
            return new_expr
        else:
            return node

class ASTModifierNameFixer(ASTModifierBase):
    '''Replace '.' in IDs with '__' '''
    def __init__(self, ast):
        ASTModifierBase.__init__(self, modify_expressions = True)
        self.traverse(ast)

    def replace_dots_in_name(self, name):
        '''Replaces . with __ in name'''
        return name.replace('.', '__')

    def modifyID(self, node, *args, **kwargs):
        orig = node.identifier
        x = self.replace_dots_in_name(node.identifier)

        if '.' in node.identifier:
            node.identifier = node.identifier.replace('.', '__')

        return node

class ASTModifierFunctionExpander(ASTModifierBase):
    '''Handle function usage'''
    def __init__(self, ast):
        ASTModifierBase.__init__(self, modify_expressions = True)
        self.traverse(ast, parent_toplevel = None, function_stack = [])

    def modifyModule(self, node, *args, **kwargs):
        '''Add init callback if it is not already available and move it to the top (before all other functions and callbacks)'''
        # find and extract 'on init' block
        on_init_block = None

        for b in node.blocks:
            if isinstance(b, ksp_ast.Callback) and b.name == 'init':
                node.blocks.remove(b)
                on_init_block = b
                break

        # if there was none, create one
        if on_init_block is None:
            on_init_block = ksp_ast.Callback(node.lexinfo, 'init', lines=[])

        # insert it as the first block
        node.blocks.insert(0, on_init_block)

        return ASTModifierBase.modifyModule(self, node, *args, **kwargs)

    def modifyFunctionDef(self, node, parent_toplevel = None, function_stack = None):
        ''' Add to context info about which function/callback we are currently inside '''
        # only functions without parameters/return vaue can be invoked using 'call'
        # those are the only ones where we need to modify the original function definition
        # (in the other cases the recursive inlining handles everything)
        if (node.parameters or node.return_value) and not node.is_taskfunc:
            return node
        else:
            return ASTModifierBase.modifyFunctionDef(self, node, parent_toplevel = node, function_stack = function_stack)

    def modifyCallback(self, node, parent_toplevel = None, function_stack = None):
        '''Add to context info about which function/callback we are currently inside'''
        return ASTModifierBase.modifyCallback(self, node, parent_toplevel = node, function_stack = function_stack)

    def convert_property_access_to_function_call(self, node):
        '''Convert a property reference like myprop to a function call like myprop.get()'''
        assert(isinstance(node, ksp_ast.VarRef))

        func_name = '%s.get' % node.identifier.identifier

        if func_name not in functions:
            raise ksp_ast.ParseException(node, 'The property %s has no get function, therefore it cannot be written to!' % str(node.identifier.identifier))

        get_function = functions[func_name]

        # if there is a subscript, pass it as a parameter to the get function
        parameters = node.subscripts[:]

        return ksp_ast.FunctionCall(node.lexinfo, get_function.name, parameters, is_procedure=False)

    def modifyVarRef(self, node, *args, **kwargs):
        '''If the VarRef is a property, then convert it to a call to the get-function of the property'''
        if node.identifier.identifier in properties:
            return self.modifyFunctionCall(self.convert_property_access_to_function_call(node),
                                           *args, **kwargs)
        else:
            return ASTModifierBase.modifyVarRef(self, node, *args, **kwargs)

    def modifyAssignStmt(self, node, parent_toplevel = None, function_stack = None, disallow_function_in_rhs = False):
        ''' If it's an assignment of the type "x := myfunc(...)" then pass the left hand side of the assignment ("x" in this case)
            as a parameter to the function call handler. This effectively passes the lhs as a parameter to the function and replaces
            the whole assignment by the inlined function body. This allows myfunc to be a multi-line function.
            Please note that this only applies if the function call is the only thing on the right hand side.
            In the case of for example "x := myfunc(...) + 1" the inlining of the function is handled in the context of
            an expression handler and in that context only functions whose body consists of a single assignment are allowed (the right
            hand side expression of that assignment is then what gets inlined into the expression. '''

        if not isinstance(node.varref, ksp_ast.VarRef):
            raise ksp_ast.ParseException(node, 'The left hand side of the assignment needs to be a variable reference!')

        # if this is a property assignment, eg. myproperty := 5, convert it to a function call, eg. myproperty.set(5)
        if node.varref.identifier.identifier in properties:
            func_name = '%s.set' % node.varref.identifier.identifier

            if func_name not in functions:
                raise ksp_ast.ParseException(node.varref, 'The property %s has no set function, therefore it is read-only!' % str(node.varref.identifier.identifier))

            set_function = functions[func_name]
            parameters = node.varref.subscripts + [node.expression]
            function_call = ksp_ast.FunctionCall(node.lexinfo, set_function.name, parameters, is_procedure = True)

            return self.modifyFunctionCall(function_call,
                                           parent_toplevel = parent_toplevel,
                                           function_stack = function_stack)

        expression = node.expression

        # if the right-hand-side is a property access, convert it to a function call to the get-function of the property
        if isinstance(expression, ksp_ast.VarRef) and expression.identifier.identifier in properties:
            expression = self.convert_property_access_to_function_call(node.expression)

        # if the right-hand-side is function call
        if isinstance(expression, ksp_ast.FunctionCall) and expression.function_name.identifier not in ksp_builtins.functions and not disallow_function_in_rhs:
            # invocations of built-in functions are not checked at this compilation stage
            return self.modifyFunctionCall(expression,
                                           parent_toplevel = parent_toplevel,
                                           function_stack = function_stack,
                                           assign_stmt_lhs = node.varref)
        else:
            return ASTModifierBase.modifyAssignStmt(self, node, parent_toplevel = parent_toplevel, function_stack = function_stack)

    def doFunctionCallChecks(self, node, function_name, func, is_inside_init_callback, function_stack, assign_stmt_lhs):
        ''' Make various checks that a function call is correct (e.g. function exists, parameters match, not recursive, invoked from a valid context).
            Factored out from the modifyFunctionCall method in order to make the logic there easier to follow '''

        # verify that function exists
        if func is None:
            raise ksp_ast.ParseException(node, "Unknown function: %s!" % function_name)

        # verify that it's not a recursive call
        if function_name in function_stack:
            raise ksp_ast.ParseException(node, "Recursive functions calls (functions directly or indirectly calling themselves) are not allowed! %s" % ' -> '.join(function_stack + [function_name]))

        # verify that number of parameters and arguments matches
        if len(node.parameters) != len(func.parameters):
            raise ksp_ast.ParseException(node, "Wrong number of parameters for %s()! Expected %d, got %d." % (function_name, len(func.parameters), len(node.parameters)))

        # verify that function calls that are part of expressions invoke a function for which a return value variable has been defined
        if not node.is_procedure and func.return_value is None:
            raise ksp_ast.ParseException(node, "Function %s does not return any value and cannot be used in this context!" % function_name)

        # verify that function is not used within some expression, unless it's a taskfunc function and it's the single thing on the right-hand side of an assignment
        if not node.is_procedure and func.is_taskfunc and assign_stmt_lhs is None:
            raise ksp_ast.ParseException(node, 'When used in an expression like this, the function call needs to be the only thing on the right hand side of an assignment! For example: x := call myfunc().')

        # verify that 'call' is not used from within 'on init'
        if is_inside_init_callback and (node.using_call_keyword or func.is_taskfunc):
            raise ksp_ast.ParseException(node, 'Usage of "call" inside init callback is not allowed! Please also verify that the called function is not a taskfunc.')

        if node.using_call_keyword:
            # verify that there are no parameters (unless it's a taskfunc function call)
            if not func.is_taskfunc and node.parameters:
                raise ksp_ast.ParseException(node, 'Functions with parameters called with "call" keyword are not supported by Kontakt!')

            # verify that 'call' is not used within some expression (eg. as in the incorrect "x := call myfunc")
            if not node.is_procedure and not func.is_taskfunc:
                raise ksp_ast.ParseException(node, 'Using "call" inside an expression is not allowed for functions other than taskfuncs! "call" needs to be the first word of the line.')

            if func.is_taskfunc:
                raise ksp_ast.ParseException(node, 'A taskfunc cannot be invoked using the "call" keyword!')

    def updateCallGraph(self, node, parent_toplevel = None, function_stack = None):
        '''This function takes a function call as parameter node and updates the call graph accordingly'''

        if isinstance(parent_toplevel, ksp_ast.FunctionDef):
            parent_function_name = parent_toplevel.name.identifier
        else:
            parent_function_name = None   # if called from within a callback represent the callback as None in the call graph (since it's not really relevant from which callback)

        function_name = node.function_name.identifier

        if function_name not in ksp_builtins.functions:

            if function_name not in functions:
                raise ksp_ast.ParseException(node.function_name, "Unknown function: %s!" % function_name)

            call_graph[parent_function_name].append(function_name)  # enter a link from the caller to the callee in the call graph
            call_graph[function_name] = call_graph[function_name]   # add target node if it doesn't already exist

            if node.using_call_keyword:
                called_functions.add(function_name)

    def getTaskFuncCallPrologueAndEpilogue(self, node, func, assign_stmt_lhs):
        '''if the function call is of the format "x := myfunc(...)" then treat it like myfunc(..., x), i.e. insert the left hand side of the assignment as the last parameter'''

        if assign_stmt_lhs:
            parameters = node.parameters + [assign_stmt_lhs]
        else:
            parameters = node.parameters

        prologue = []
        epilogue = []

        num_params = len(parameters)

        # "virtually" increase parameter count so that if taskfunc has a return value,
        # but upon calling is not assigned to a variable to store the return into,
        # it would still use the correct stack pointer offset.
        # See issue #217 on GitHub!
        if func.return_value is not None and not assign_stmt_lhs:
            num_params = num_params + 1

        for i, param in enumerate(parameters):
            idx = num_params - i

            if func.parameter_types[i] not in ('out', 'ref'):
                li = param.lexinfo
                p_ref = ksp_ast.VarRef(li, ksp_ast.ID(li, '%p'),
                                       [ksp_ast.BinOp(li, ksp_ast.VarRef(li, ksp_ast.ID(li, '$sp')), '-', ksp_ast.Integer(li, idx))])
                prologue.append(ksp_ast.AssignStmt(li, p_ref, param))

            if isinstance(param, ksp_ast.VarRef) and func.parameter_types[i] in ('out', 'var'):
                li = param.lexinfo
                p_ref = ksp_ast.VarRef(li, ksp_ast.ID(li, '%p'),
                                       [ksp_ast.BinOp(li, ksp_ast.VarRef(li, ksp_ast.ID(li, '$sp')), '-', ksp_ast.Integer(li, idx))])
                epilogue.append(ksp_ast.AssignStmt(li, param, p_ref))

        return (prologue, epilogue)

    def modifyFunctionCall(self, node, parent_toplevel = None, function_stack = None, assign_stmt_lhs = None):
        ''' For invocations of user-defined functions check that the function is defined and that the number of parameters match.
            Unless "call" is used inline the function '''

        function_name = node.function_name.identifier  # shorter name alias

        # update call graph
        self.updateCallGraph(node, parent_toplevel, function_stack)

        # invocations of built-in functions are not checked at this compilation stage
        if function_name in ksp_builtins.functions                                                                                \
           and not (function_name in functions and function_name in ksp_builtins.functions and functions[function_name].override) \
           and not node.using_call_keyword:

            if function_name == 'wait' and isinstance(parent_toplevel, ksp_ast.FunctionDef):
                functions_invoking_wait.add(parent_toplevel.name.identifier)

            return ASTModifierBase.modifyFunctionCall(self, node, parent_toplevel = parent_toplevel, function_stack = function_stack, assign_stmt_lhs = assign_stmt_lhs)

        # get a reference to the function node and run error checks
        func = functions.get(function_name, None)
        is_inside_init_callback = isinstance(parent_toplevel, ksp_ast.Callback) and parent_toplevel.name == 'init'
        self.doFunctionCallChecks(node, function_name, func, is_inside_init_callback, function_stack, assign_stmt_lhs)

        # if function invoked from within a callback, mark it as used
        if isinstance(parent_toplevel, ksp_ast.Callback):
            functions[function_name].used = True

        # if it's a call to a taskfunc function
        if func.is_taskfunc:
            (prologue, epilogue) = self.getTaskFuncCallPrologueAndEpilogue(node, func, assign_stmt_lhs)
            prologue = flatten([self.modifyAssignStmt(stmt, parent_toplevel, function_stack, disallow_function_in_rhs = True) for stmt in prologue])
            epilogue = flatten([self.modifyAssignStmt(stmt, parent_toplevel, function_stack, disallow_function_in_rhs = True) for stmt in epilogue])
            result = prologue + [node] + epilogue
            node.parameters = []
            node.using_call_keyword = True
            node.is_procedure = True
            called_functions.add(node.function_name.identifier)

        # if 'call' keyword is used
        elif node.using_call_keyword:
            result = [node]

        # else if we're inlining the function start out with an empty result since the line of invocation itself will be replaced
        else:
            result = []
            if is_inside_init_callback:
                # inline any declarations made inside the body of the invoked function (afterwards clear the lists so that they don't get inlined twice)
                result = func.global_declaration_statements + func.local_declaration_statements + result
                func.local_declaration_statements = []
                func.global_declaration_statements = []

            # build a substitution dictionary that maps parameters to arguments
            name_subst_dict = dict(list(zip(list(map(str, func.parameters)), node.parameters)))
            # if the function call is of the format "x := myfunc(...)", then setup the dict up to substitute the result variable of the function by "x"
            if assign_stmt_lhs:
                name_subst_dict[str(func.return_value)] = assign_stmt_lhs
            # also add the mapping from local variable names to their new globally unique counterpart
            name_subst_dict.update(func.locals_name_subst_dict)

            # apply name substitutions to a copy of the function
            func = ASTModifierVarRefSubstituter(name_subst_dict, inlining_function_node = node).modify(func.copy())

            # recursively modify each line in the function body and add them to the return value
            result = result + flatten([self.modify(line, parent_toplevel = parent_toplevel, function_stack = function_stack + [function_name]) for line in func.lines])

            # if this function call is embedded within some expression
            if not (assign_stmt_lhs or node.is_procedure):
                # if the inlined function body consists of just a single statement on the format: result := <expr> where 'result' is the result variable of the function
                if len(result) == 1 and isinstance(result[0], ksp_ast.AssignStmt) and result[0].varref.identifier.identifier == func.return_value.identifier:
                    return result[0].expression  # return the right-hand side of that single assignment
                else:
                    raise ksp_ast.ParseException(node, \
                          'The definition of function %s needs to consist of a single line (e.g. "result := <expr>") in order to be used in this context!' % function_name)

        return result

class ASTModifierTaskfuncFunctionHandler(ASTModifierBase):
    '''Handle Taskfunc Nodes'''

    def __init__(self, ast):
        ASTModifierBase.__init__(self, modify_expressions = False)
        self.traverse(ast, parent_taskfunc_function = None)

    def modifyCallback(self, node, *args, **kwargs):
        return node

    def modifyFunctionDef(self, node, parent_taskfunc_function = None, assign_stmt_lhs = None):
        # Add to context info about which taskfunc function we are currently inside
        ID, BinOp, Integer, VarRef, AssignStmt, FunctionCall = ksp_ast.ID, ksp_ast.BinOp, ksp_ast.Integer, ksp_ast.VarRef, ksp_ast.AssignStmt, ksp_ast.FunctionCall

        if not node.is_taskfunc:
            return node

        if node.return_value:
            params = node.parameters + [node.return_value]
        else:
            params = node.parameters

        # build a substitution dictionary that maps parameters to arguments
        name_subst_dict = {}
        li = node.lexinfo

        for i, param in enumerate(params):
            frame_offset = i + len(node.taskfunc_declaration_statements) + 1

            # replace locally declared 'x' with '%p[$fp + <var_index>'
            p_ref = VarRef(li, ID(li, '%p'), [BinOp(li, VarRef(li, ID(li, '$fp')), '+', Integer(li, frame_offset))])
            name_subst_dict[str(param)] = p_ref

        # apply name substitutions to a copy of the function
        func = ASTModifierVarRefSubstituter(name_subst_dict, subscript_addition = False).modify(node)

        # add prolog
        Pxmax = len(params)
        Txmax = len(node.taskfunc_declaration_statements)
        Ta = Pxmax + Txmax + 1

        line0 = AssignStmt(li, VarRef(li, ID(li, '%p'), [BinOp(li, VarRef(li, ID(li, '$sp')), '-', Integer(li, Ta))]), VarRef(li, ID(li, '$fp')))
        line1 = AssignStmt(li, VarRef(li, ID(li, '$fp')), BinOp(li, VarRef(li, ID(li, '$sp')), '-', Integer(li, Ta)))
        line2 = AssignStmt(li, VarRef(li, ID(li, '$sp')), VarRef(li, ID(li, '$fp')))

        node.lines.insert(0, line0)
        node.lines.insert(1, line1)
        node.lines.insert(2, line2)

        if 'TCM_DEBUG' in true_conditions:
            line3 = FunctionCall(li, function_name = ID(li, 'check_full'), parameters = [], is_procedure = True, using_call_keyword = True)
            node.lines.insert(3, line3)
            call_graph[node.name.identifier].append('check_full')
            called_functions.add('check_full')

        # epilogue
        line0 = AssignStmt(li, VarRef(li, ID(li, '$sp')), VarRef(li, ID(li, '$fp')))
        line1 = AssignStmt(li, VarRef(li, ID(li, '$fp')), VarRef(li, ID(li, '%p'), [VarRef(li, ID(li, '$fp'))]))
        line2 = AssignStmt(li, VarRef(li, ID(li, '$sp')), BinOp(li, VarRef(li, ID(li, '$sp')), '+', Integer(li, Ta)))

        node.lines.append(line0)
        node.lines.append(line1)
        node.lines.append(line2)

        node.parameters = []

        return func

class ASTModifierFixPrefixesAndFixControlPars(ASTModifierFixPrefixes):
    '''Checks prefixs and control_pars. Add get_ui_id() to control_pars'''

    def __init__(self, ast):
        ASTModifierFixPrefixes.__init__(self, ast)

    def modifyVarRef(self, node, *args, **kwargs):
        '''Check that there is not more than one subscript'''
        if len(node.subscripts) > 1:
            raise ksp_ast.ParseException(node.subscripts[0], 'Too many variable subscripts: %s! A normal array variable can have at most one.' % str(node))

        return ASTModifierFixPrefixes.modifyVarRef(self, node, *args, **kwargs)

    def modifyFunctionCall(self, node, *args, **kwargs):
        result = ASTModifierFixPrefixes.modifyFunctionCall(self, node, *args, **kwargs)

        if node.is_procedure:
            node = result[0]
        else:
            node = result

        # shorter name alias
        if not isinstance(node, ksp_ast.FunctionCall):
            return node

        function_name = node.function_name.identifier

        # if it's an event_par which does not have a constant return then raise error
        if function_name in ksp_builtins.functions and not node.using_call_keyword                      \
           and (function_name.startswith('set_event_par') or function_name.startswith('get_event_par')) \
           and isinstance(node.parameters[0], ksp_ast.FunctionCall)                                     \
           and str(node.parameters[0].function_name) not in ksp_builtins.functions_with_constant_return:
            raise ksp_ast.ParseException(node.parameters[0], '"%s" is not a valid as a function that can be used with set/get event_pars!' % node.parameters[0].function_name)

        # if it's a builtin function that sets or gets a control par and the first parameter is not an integer ID, but rather a UI variable
        if function_name in ksp_builtins.functions and not node.using_call_keyword                           \
           and (function_name.startswith('set_control_par') or function_name.startswith('get_control_par'))  \
           and len(node.parameters) > 0 and isinstance(node.parameters[0], ksp_ast.VarRef)                   \
           and str(node.parameters[0].identifier).lower() in ui_variables:

            # then wrap the UI variable in a get_ui_id call, eg. myknob is converted into get_ui_id(myknob)
            func_call_inner = ksp_ast.FunctionCall(node.lexinfo, ksp_ast.ID(node.parameters[0].lexinfo, 'get_ui_id'), [node.parameters[0]], is_procedure = False)

            # If last parameter is a ui_variable and is trying to add to parent_panel then add get_ui_id() wrapper
            if function_name.startswith('set_control_par') and str(node.parameters[1]).endswith("PARENT_PANEL")  \
               and isinstance(node.parameters[2], ksp_ast.VarRef)                                                \
               and str(node.parameters[2].identifier).lower() in ui_variables:

                func_call_outer = ksp_ast.FunctionCall(node.lexinfo, ksp_ast.ID(node.parameters[2].lexinfo, 'get_ui_id'), [node.parameters[2]], is_procedure = False)
                node.parameters[2] = func_call_outer

            node = ksp_ast.FunctionCall(node.lexinfo, node.function_name, [func_call_inner] + node.parameters[1:], is_procedure = node.is_procedure)

        if node.is_procedure:
            return [node]
        else:
            return node

def mark_used_functions_using_depth_first_traversal(call_graph, start_node = None, visited = None):
    ''' Make a depth-first traversal of call graph and set the used attribute of functions invoked directly or indirectly from some callback.
        The graph is represented by a dictionary where graph[f1] == f1 means that the function with name f1 calls the function with name f2 (the names are strings).'''

    if visited is None:
        visited = set()

    nodes_to_visit = set()

    if start_node is None:
        nodes_to_visit = set(call_graph[None])  # None represents the source of a normal callback (a callback invoking a function as opposed to a function invoking a function)
    else:
        if start_node not in visited:
            functions[start_node].used = True
            visited.add(start_node)
            nodes_to_visit = set([x for x in call_graph[start_node] if x is not None])

    for n in nodes_to_visit:
        mark_used_functions_using_depth_first_traversal(call_graph, n, visited)

def find_node(start_node, search_node, visited = None, path = None):
    if visited is None:
        visited = []
        path = []

    visited.append(start_node)
    path = path + [start_node]

    if start_node == search_node:
        return path

    for n in start_node.get_childnodes():
        find_node(n, search_node, visited, path)

    return path

def find_cycles(graph, ready = None):
    # adapted from code at http://neopythonic.blogspot.com/2009/01/detecting-cycles-in-directed-graph.html
    if ready is None:
        ready = set()

    todo = set(graph.keys())

    while todo:
        node = todo.pop()
        stack = [node]

        while stack:
            top = stack[-1]

            for node in graph[top]:
                if node in stack:
                    raise Exception('Recursion detected! ' + '->'.join(map(str, stack + [node])))

                if node in todo:
                    stack.append(node)
                    todo.remove(node)
                    break
            else:
                node = stack.pop()
        ready.add(node)

    return None

def topological_sort(graph):
    # copied from here: http://www.logarithmic.net/pfh-files/blog/01208083168/sort.py
    count = {}

    # TODO: maybe replace None with empty string '' to make sorting easier
    nodes = ['' if k is None else k for k in graph.keys()]
    nodes.sort()
    nodes = [None if k == '' else k for k in nodes]

    for node in nodes:
        count[node] = 0

    for node in nodes:
        for successor in graph[node]:
            count[successor] += 1

    ready = [node for node in nodes if count[node] == 0]

    result = []

    while ready:
        node = ready.pop(-1)
        result.append(node)

        for successor in graph[node]:
            count[successor] -= 1

            if count[successor] == 0:
                ready.append(successor)

    return result

def compress_variable_name(name):
    symbols = 'abcdefghijklmnopqrstuvwxyz012345'
    hash = hashlib.new('sha1')
    hash.update(name.encode('utf-8'))

    return ''.join((symbols[ch & 0x1F] for ch in hash.digest()[:5]))

def parse_nckp(path):
    '''
    The prefix list is ordered to match the integer number representing each ui_control:

    0. panel
    1. button
    2. file selector
    3. knob
    4. label
    5. level meter
    6. menu
    7. slider
    8. switch
    9. table
    10. text edit
    11. value edit
    12. waveform
    13. wavetable
    14. xy pad
    15. mouse area
    '''

    prefix = ["$", "$", "$", "$", "$", "$", "$", "$", "$", "%", "@", "$", "$", "$", "?", "$"]
    cur_prefix = []

    # iterate recursively into the nested data
    def search_ui_in_dict_recursively(dictionary):
        tree = ""
        name = ""
        key = 'index'

        for k, v in dictionary.items():
            if k == key:
                cur_prefix.append(prefix[v])
                tree = dictionary["value"]["common"]["id"]

                yield tree
            elif isinstance(v, dict):
                for result in search_ui_in_dict_recursively(v):
                    name = (tree + "_" + result) if tree else result

                    yield name
            elif isinstance(v, list):
                for d in v:
                    for result in search_ui_in_dict_recursively(d):
                        yield result

    with open(path, 'r') as read_file:
        data = json.load(read_file, object_pairs_hook = OrderedDict)

    ui_controls_names = list(search_ui_in_dict_recursively(data))

    # pair the collected prefix to the parsed ui_control names
    for i, p in enumerate(ui_controls_names):
        yield cur_prefix[i] + p

def open_nckp(lines, basedir):
    source = merge_lines(lines) # for checking purposes
    nckp_path = '' # predeclared to avoid errors if the import_nckp ksp function is not used
    ui_to_import = []

    for index, l in enumerate(lines):
        line = l.command

        if 'import_nckp' in line:
            if 'load_performance_view' in source:
                if 'make_perfview' in source:
                    raise ParseException(Line(line, [(None, index + 1)], None), 'If \'load_performance_view\' is used, \'make_perfview\' must be removed!\n')

                nckp_path = line[line.find('(') + 1:line.find(')')][1:-1]

                if nckp_path:
                    # normalize the extracted path so that it works on Mac if it has backward slashes
                    nckp_path = nckp_path.replace("\\", "/")

                    # check if the path is relative or not
                    if not os.path.isabs(nckp_path):
                        nckp_path = os.path.join(basedir, nckp_path)

                    if os.path.exists(nckp_path):
                        ui_to_import = list(parse_nckp(nckp_path))

                        if not ui_to_import:
                            utils.log_message("Note: No UI control declarations were found in %s!" % os.path.abspath(nckp_path))
                            strip_import_nckp_function_from_source(lines)

                        for i, v in enumerate(ui_to_import):
                            variables.add(v.lower())
                            ui_variables.add(v.lower())
                            comp_extras.add_nckp_var_to_nckp_table(v)

                            # Support the use of '.' variables in the compiler to reference controls with double underscores
                            variables.add(v.lower().replace('__', '.'))
                            ui_variables.add(v.lower().replace('__', '.'))
                            comp_extras.add_nckp_var_to_nckp_table(v.replace('__', '.'))

                    else:
                        raise ParseException(Line(line, [(None, index + 1)], None), '.nkcp file not found at: %s!' % os.path.abspath(nckp_path))
            else:
                raise ParseException(Line(line, [(None, index + 1)], None), 'import_nckp was used, but \'load_performance_view\' was not found in the script!\n')

    return bool(ui_to_import)

def strip_import_nckp_function_from_source(lines):
    for line_obj in lines:
        line = line_obj.command
        ls_line = line.lstrip()

        if 'import_nckp' in ls_line:
            line_obj.command = re.sub(r'[^\r\n]', '', ls_line)

class KSPCompiler(object):
    def __init__(self,
                 source,
                 basedir,
                 compact                        = True,
                 compact_variables              = False,
                 combine_callbacks              = False,
                 extra_syntax_checks            = False,
                 optimize                       = False,
                 additional_branch_optimization = False,
                 sanitize_exit_command          = False,
                 add_compiled_date_comment      = False,
                 force_compiler_arguments       = False,
                 write_log_on_fail              = False,
                 compiled_code_tab_size         = 2):

        self.source = source
        self.basedir = basedir
        self.compact = compact
        self.compact_variables = compact_variables
        self.optimize = optimize
        self.additional_branch_optimization = additional_branch_optimization
        self.sanitize_exit_command = sanitize_exit_command
        self.combine_callbacks = combine_callbacks
        self.add_compiled_date_comment = add_compiled_date_comment
        self.force_compiler_arguments = force_compiler_arguments
        self.write_log_on_fail = write_log_on_fail
        self.compiled_code_tab_size = compiled_code_tab_size
        self.extra_syntax_checks = extra_syntax_checks or optimize

        self.abort_requested = False

        self.module = None
        self.define_cache = None

        self.lines = []
        self.macros = []
        self.output_files = []

        self.original2short = {}
        self.short2original = {}

        self.variable_names_to_preserve = set()
        self.compiler_options_to_override = dict()

        self.compiler_import_cache = []

    def do_imports_and_convert_to_line_objects(self):
        # Import files
        self.lines = parse_lines_and_handle_imports(self.basedir,
                                                    self.source,
                                                    self.compiler_import_cache,
                                                    preprocessor_func = self.examine_pragmas)

        # Parse conditionals and remove lines if appropriate
        handle_conditional_lines(self.lines)

    # PAST THIS FUNCTION, ALL IMPORTED AND UPDATED CODE LIVES IN SELF.LINES, NOT SOURCE. DO NOT ATTEMPT TO REPRODUCE LINE OBJECTS FROM SOURCE
    # TO PRESERVE LINE PROPERTIES, SELF.LINES CAN NOT BE REMERGED INTO SOURCE

    def extensions_with_macros(self):
        '''Replaces lines with relevant preprocessor plugin code, ike Task Control Module'''
        check_lines = [copy.copy(l) for l in self.lines]

        for i, line in enumerate(check_lines):
            line.replace_placeholders()

            if 'activate_logger' in line.command:
                raise ParseException(self.lines[i],
                                     'Logger functionality has been removed!\n\n' +
                                     'Please consider updating your script to use watch_var() and watch_array_idx() commands instead!')

        check_source = merge_lines(check_lines) # only for checking purposes, not for reproducing lines

        # Add TCM code if tcm.init() is found
        if re.search(r'(?m)^\s*tcm.init', check_source):
            self.lines += parse_lines_and_handle_imports(self.basedir,
                                                         taskfunc_code,
                                                         self.compiler_import_cache,
                                                         preprocessor_func = self.examine_pragmas)

        # Run conditional stage a second time to catch the new source additions.
        handle_conditional_lines(self.lines)

    def search_for_nckp(self):
        '''Import nckp if import_nckp() found'''
        if open_nckp(self.lines, self.basedir):
            strip_import_nckp_function_from_source(self.lines)

    def replace_string_placeholders(self):
        for line in self.lines:
            line.replace_placeholders()

    # NOTE(Sam): Previously done in the expand_macros function, the lines are converted into a block in separately
    # because the preprocessor needs to be called after the macros and before this.
    def convert_lines_to_code(self):
        self.code = merge_lines(self.lines)

    def run_pre_macro_functions(self):
        '''Create define cache and run pre_macro_functions from `preprocessor_plugins.py`'''
        from preprocessor_plugins import pre_macro_functions

        self.define_cache = pre_macro_functions(self.lines)

    def run_post_macro_functions(self):
        '''Run post_macro_functions from `preprocessor_plugins.py`'''
        from preprocessor_plugins import post_macro_functions, handleStringArrayInitialisation, handleArrayConcat

        post_macro_functions(self.lines)
        handleStringArrayInitialisation(self.lines, placeholders)
        handleArrayConcat(self.lines)

    def run_sanitize_exit_command(self):
        '''Run handleSanitizeExitCommand from `preprocessor_plugins.py`'''
        from preprocessor_plugins import handleSanitizeExitCommand

        handleSanitizeExitCommand(self.lines)

    def extract_macros(self):
        '''Isolate macros into objects, removing from code'''
        self.lines, self.macros = extract_macros(self.lines)

    def expand_macros(self):
        from preprocessor_plugins import macro_iter_functions, post_macro_iter_functions, substituteDefines

        normal_lines, callback_lines = expand_macros(self.lines, self.macros, 0, True, self.define_cache)
        self.lines = normal_lines + callback_lines

        convert_strings_to_placeholders(self.lines)

        while macro_iter_functions(self.lines, placeholders):
            normal_lines, callback_lines = expand_macros(self.lines, self.macros, 0, True, self.define_cache)
            self.lines = normal_lines + callback_lines

        convert_strings_to_placeholders(self.lines)

        while post_macro_iter_functions(self.lines, placeholders):
            normal_lines, callback_lines = expand_macros(self.lines, self.macros, 0, True, self.define_cache)
            self.lines = normal_lines + callback_lines

    def examine_pragmas(self, code, namespaces):
        '''Examine pragmas within code'''

        # find path to where we will save the compiled code
        pragma_re = re.compile(r'\{\s*\#pragma\s+save_compiled_source\s+(.*)\}')

        for m in pragma_re.finditer(code):
            dir_check = m.group(1).strip()

            if not os.path.isabs(dir_check):
                if self.basedir == None:
                    raise Exception('Please save the file to hard drive before attempting to compile to a relative path!')

                dir_check = os.path.join(self.basedir, dir_check)

            dir_path = os.path.dirname(dir_check)

            if os.path.exists(dir_path) or (dir_path == '' and dir_check.endswith('.txt')):
                self.output_files.append(dir_check)
            else:
                raise Exception('The filepath specified in save_compiled_source does not exist!\n' + dir_path)

        # find info about which variable names not to compact
        pragma_re = re.compile(r'\{\s*\#pragma\s+preserve_names\s+(.*?)\s*\}')

        for m in pragma_re.finditer(code):
            names = re.sub(r'[$!%@?~]', '', m.group(1))  # remove any prefixes

            for variable_name_pattern in re.split(r'\s+,?\s*|\s*,\s+|,', names):
                if len(variable_name_pattern) == 0:
                    continue

                if namespaces:
                    variable_name_pattern = '__'.join(namespaces + [variable_name_pattern])

                variable_name_pattern = variable_name_pattern.replace('.', '__').replace('*', '.*')
                self.variable_names_to_preserve.add(variable_name_pattern)

        # compiler option overrides
        for m in pragma_compile_with_re.finditer(code):
            if m.group(1) not in self.compiler_options_to_override:
                self.compiler_options_to_override[m.group(1)] = True

        for m in pragma_compile_without_re.finditer(code):
            if m.group(1) not in self.compiler_options_to_override:
                self.compiler_options_to_override[m.group(1)] = False

        return code

    def parse_code(self):
        '''Parse code with ply lex/yacc'''
        self.module = parse(self.code, self.lines)

    def sort_functions_and_insert_local_variables_into_on_init(self):
        '''Ensures no recusion with functions invoked with 'call'\n
           Do a topological sort for functions. \n
           Move local variables to on init if not already inserted '''

        # make sure that used function that uses others set the used flag of those secondary ones as well
        used_functions = set()

        mark_used_functions_using_depth_first_traversal(call_graph, visited = used_functions)

        # check that there is no recursion among functions invoked using 'call'
        find_cycles(call_graph)

        # make a topological sorting of the call graph filter out the functions invoked using 'call'
        function_definition_order = [function_name
                                     for function_name in reversed(topological_sort(call_graph))
                                     if function_name in called_functions and function_name in used_functions]

        # create a lookup table from function name to function definition, remove all function definitions
        # and then add the ones used back in the right order (as determined by the topological sorting)
        function_table = dict([(func.name.identifier, func) for func in self.module.blocks if isinstance(func, ksp_ast.FunctionDef)])
        self.module.blocks = [block for block in self.module.blocks if not isinstance(block, ksp_ast.FunctionDef)]
        self.module.blocks = [self.module.on_init] + [function_table[func_name] for func_name in function_definition_order] + self.module.blocks[1:]

        # add local variable declarations to 'on init' in case they have not already been inserted
        # (they could have been inserted earlier if the function was invoked from the init callback)
        for f in reversed(list(functions.values())):
            if f.used and (f.global_declaration_statements or f.local_declaration_statements):
                self.module.on_init.lines = f.global_declaration_statements + self.module.on_init.lines + f.local_declaration_statements
                f.global_declaration_statements = []
                f.local_declaration_statements = []

    def convert_dots_to_double_underscore(self):
        '''Convert all dots into '__' (and update the list of variables accordingly)
           Note: for historical reasons the ksp_compiler_extras functions assume
           pure KSP as input and therefore cannot handle '.' in names.'''

        global variables

        # update the AST
        name_fixer = ASTModifierNameFixer(self.module)
        # update the global list of variables similarly
        variables = set(name_fixer.replace_dots_in_name(v) for v in variables)

    def compact_names(self):
        global variables

        # build regular expression that can later tell which names to preserve (these should not undergo compaction)
        preserve_pattern = re.compile(r'[$%@!?~]?(' + '|'.join(self.variable_names_to_preserve) + ')$', re.I)

        for v in variables:
            if self.variable_names_to_preserve and preserve_pattern.match(v):
                continue
            elif v not in self.original2short and v not in ksp_builtins.all_builtins:
                self.original2short[v] = '%s%s' % (v[0], compress_variable_name(v))
                if self.original2short[v] in ksp_builtins.all_builtins:
                    raise Exception('This is your unlucky day! Even though the chance is only 0.32%%, the variable %s was mapped to the same hash as that of a builtin KSP variable.' % (v))
                if self.original2short[v] in self.short2original:
                    raise Exception('This is your unlucky day! Even though the chance is only 0.32%%, two variable names were compacted to the same short name: %s and %s.' % (v, self.short2original[self.original2short[v]]))
                self.short2original[self.original2short[v]] = v

        ASTModifierIDSubstituter(self.original2short, force_lower_case = True).modify(self.module)

    def init_extra_syntax_checks(self):
        comp_extras.clear_symbol_table()
        self.used_variables = set()
        self.var_assigns = {}

    def generate_compiled_code(self):
        '''Generate compiled code from AST'''

        buffer = StringIO()
        emitter = ksp_ast.Emitter(buffer, compact = self.compact, compiled_code_tab_size = self.compiled_code_tab_size)
        self.module.emit(emitter)
        self.compiled_code = buffer.getvalue()

        lines = self.compiled_code.split('\n')

        new_lines = []
        if self.add_compiled_date_comment:
            localtime = time.asctime( time.localtime(time.time()) )
            new_lines.append("{ Compiled on " + localtime + " }")

        init_block = False
        for l in lines:
            new_lines.append(l)

        self.compiled_code = '\n'.join(new_lines)

    def uncompress_variable_names(self, compiled_code):
        def sub_func(match_obj):
            s = match_obj.group(0)

            if s in self.short2original:
                return self.short2original[s]
            else:
                return s

        return varname_re.sub(sub_func, compiled_code)

    def compile(self, callback = None):
        global variables

        clear_global_context()

        compiled_code = []
        try:
            used_functions = set()
            used_variables = set()
            var_assigns = {}

            tasks_executed = 0

            if self.abort_requested:
                return False

            # do the code scanning and importing as a separate step in order to capture "compile_with" pragmas
            # so that we can override compiler options downstream
            if callback:
                callback('scanning and importing code', tasks_executed)

            self.do_imports_and_convert_to_line_objects()
            tasks_executed += 1

            # override compiler options through pragma directives
            # but only if --force command line option is not used
            if not self.force_compiler_arguments:
                for option, value in self.compiler_options_to_override.items():
                    if option == 'remove_whitespace':
                        self.compact = value
                    if option == 'compact_variables':
                        self.compact_variables = value
                    if option == 'combine_callbacks':
                        self.combine_callbacks = value
                    if option == 'extra_syntax_checks':
                        self.extra_syntax_checks = value
                    if option == 'optimize_code':
                        if self.extra_syntax_checks == False and value:
                            self.extra_syntax_checks = value

                        self.optimize = value
                    if option == 'extra_branch_optimization':
                        if self.extra_syntax_checks == False and value:
                            self.extra_syntax_checks = value

                        self.additional_branch_optimization = value
                    if option == 'add_compile_date':
                        self.add_compiled_date_comment = value
                    if option == 'sanitize_exit_command':
                        self.sanitize_exit_command = value
                    if option == 'write_log_on_fail':
                        self.write_log_on_fail = value

            do_extra = self.extra_syntax_checks
            do_optim = do_extra and self.optimize
            do_abo = do_extra and self.additional_branch_optimization
            do_sanitize_exit = self.sanitize_exit_command

            #      description                        function                                                                                        condition
            tasks = [
                 ('processing extensions',            lambda: self.extensions_with_macros(),                                                          True),

                 ('pre-macro processes',              lambda: self.run_pre_macro_functions(),                                                         True),

                 ('parsing macros',                   lambda: self.extract_macros(),                                                                  True),
                 ('expanding macros',                 lambda: self.expand_macros(),                                                                   True),

                 ('post-macro processes',             lambda: self.run_post_macro_functions(),                                                        True),

                 ('sanitizing exit command',          lambda: self.run_sanitize_exit_command(),                                                       do_sanitize_exit),
                 ('replacing string placeholders',    lambda: self.replace_string_placeholders(),                                                     True),
                 ('searching for nckp import',        lambda: self.search_for_nckp(),                                                                 True),
                 ('converting lines to code blocks',  lambda: self.convert_lines_to_code(),                                                           True),

                 ('parsing code',                     lambda: self.parse_code(),                                                                      True),
                 ('combining callbacks',              lambda: ASTModifierCombineCallbacks(self.module, self.combine_callbacks),                       True),
                 ('modifying nodes to native KSP',    lambda: ASTModifierNodesToNativeKSP(self.module, self.lines),                                   True),
                 ('adding variable name prefixes',    lambda: ASTModifierFixPrefixesIncludingLocalVars(self.module),                                  True),
                 ('inlining functions',               lambda: ASTModifierFunctionExpander(self.module),                                               True),
                 ('handling taskfuncs',               lambda: ASTModifierTaskfuncFunctionHandler(self.module),                                        True),
                 ('handling local variables',         lambda: self.sort_functions_and_insert_local_variables_into_on_init(),                          True),
                 ('adding variable name prefixes',    lambda: ASTModifierFixPrefixesAndFixControlPars(self.module),                                   True),
                 ('converting dots to underscores',   lambda: self.convert_dots_to_double_underscore(),                                               True),

                 ('initializing extra syntax checks', lambda: self.init_extra_syntax_checks(),                                                        do_extra),
                 ('checking expression types',        lambda: comp_extras.ASTVisitorDetermineExpressionTypes(self.module, functions),                 do_extra),
                 ('checking statement types',         lambda: comp_extras.ASTVisitorCheckStatementExprTypes(self.module),                             do_extra),
                 ('removing unused branches',         lambda: comp_extras.ASTModifierRemoveUnusedBranches(self.module),                               do_abo),
                 ('checking declarations',            lambda: comp_extras.ASTVisitorCheckDeclarations(self.module),                                   do_extra),
                 ('simplying expressions',            lambda: comp_extras.ASTModifierSimplifyExpressions(self.module, True),                          do_optim),
                 ('removing unused branches',         lambda: comp_extras.ASTModifierRemoveUnusedBranches(self.module),                               do_optim),
                 ('finding unused functions',         lambda: comp_extras.ASTVisitorFindUsedFunctions(self.module, used_functions),                   do_optim),
                 ('removing unused functions',        lambda: comp_extras.ASTModifierRemoveUnusedFunctions(self.module, used_functions),              do_optim),
                 ('finding unused variables',         lambda: comp_extras.ASTVisitorFindUsedVariables(self.module, used_variables, var_assigns),      do_optim),
                 ('removing unused variables',        lambda: comp_extras.ASTModifierRemoveUnusedVariables(self.module, used_variables, var_assigns), do_optim),

                 ('compacting variable names',        self.compact_names,                                                                             self.compact_variables),
                 ('generating code',                  self.generate_compiled_code,                                                                    True),
            ]

            # keep only tasks where the execution-condition is true
            tasks = [(desc, func) for (desc, func, condition) in tasks if condition]
            total_tasks = float(len(tasks))

            for (desc, func) in tasks:
                if self.abort_requested:
                    return False

                if callback:
                    callback(desc, 100 * tasks_executed / total_tasks)
                    compiled_code = [line.command for line in self.lines]

                func()

                tasks_executed += 1

            return True

        except ksp_ast.ParseException as e:
            messages = []

            if isinstance(e.node, lex.LexToken):
                line_numbers = [e.node.lineno]
            else:
                line_numbers = [e.node.lineno] + [n.lineno for n in e.node.lexinfo[2]]

            messages = ['%s' % str(e)]

            for indent, line_number in enumerate(line_numbers):
                line = self.lines[line_number]

            message = '\n'.join(messages)

            if self.write_log_on_fail and self.basedir:
                with open(os.path.join(self.basedir, '__compile_fail.log'), 'w') as out:
                    out.write('\n'.join(compiled_code))
            raise ParseException(line, message)
        except Exception as e:
            print("Exception encountered...")
            raise e

    def abort_compilation(self):
        self.abort_requested = True
        utils.log_message('Compilation aborted!')

def main():
    '''Using the compiler as command line tool'''
    import sys
    import argparse
    from time import strftime, localtime
    from datetime import datetime

    # definition of argsparse.FileType in Python 3.4 (with support for encoding) - in case we're running Python 3.3
    class FileType(object):
        def __init__(self, mode='r', bufsize=-1, encoding=None, errors=None):
            self._mode = mode
            self._bufsize = bufsize
            self._encoding = encoding
            self._errors = errors

        def __call__(self, string):
            # the special argument "-" means sys.std{in,out}
            if string == '-':
                if 'r' in self._mode:
                    return sys.stdin
                elif 'w' in self._mode:
                    return sys.stdout
                else:
                    msg = _('Argument "-" with mode %r') % self._mode
                    raise ValueError(msg)

            # all other arguments are used as file names
            try:
                return open(string, self._mode, self._bufsize, self._encoding, self._errors)
            except OSError as e:
                message = _("Cannot open '%s': %s")
                raise ArgumentTypeError(message % (string, e))

        def __repr__(self):
            args = self._mode, self._bufsize
            kwargs = [('Encoding', self._encoding), ('Errors', self._errors)]
            args_str = ', '.join([repr(arg) for arg in args if arg != -1] +
                                 ['%s=%r' % (kw, arg) for kw, arg in kwargs
                                  if arg is not None])
            return '%s(%s)' % (type(self).__name__, args_str)

    # parse command line arguments
    arg_parser = argparse.ArgumentParser()

    arg_parser.add_argument('-f', '--force',
                            dest = 'force_compiler_arguments', action = 'store_true', default = False,
                            help = 'force all specified compiler options, overriding any compile_with pragma directives from the script')
    arg_parser.add_argument('-c', '--compact',
                            dest = 'compact', action = 'store_true', default = False,
                            help = 'remove indents and empty lines in compiled code')
    arg_parser.add_argument('-v', '--compact_variables',
                            dest = 'compact_variables', action = 'store_true', default = False,
                            help = 'shorten and obfuscate variable names in compiled code')
    arg_parser.add_argument('-d', '--combine_callbacks',
                            dest = 'combine_callbacks', action = 'store_true', default = False,
                            help = 'combines duplicate callbacks - but not functions or macros')
    arg_parser.add_argument('-e', '--extra_syntax_checks',
                            dest = 'extra_syntax_checks', action = 'store_true', default = False,
                            help = 'additional syntax checks during compilation')
    arg_parser.add_argument('-o', '--optimize',
                            dest = 'optimize', action = 'store_true', default = False,
                            help = 'optimize the compiled code')
    arg_parser.add_argument('-b', '--extra_branch_optimization',
                            dest = 'additional_branch_optimization', action = 'store_true', default = False,
                            help = 'adds branch optimization checks earlier in compile process, allowing define constant based branching etc.')
    arg_parser.add_argument('-i', '--indent-size',
                            dest = 'num_spaces', action = 'store', type = int, default = 2,
                            help = 'specifies how many spaces is used for indentation, if --compact compiler option is not used')
    arg_parser.add_argument('-t', '--add_compile_date',
                            dest = 'add_compile_date', action = 'store_true', default = False,
                            help = 'adds the date and time comment atop the compiled code')
    arg_parser.add_argument('-x', '--sanitize_exit_command',
                            dest = 'sanitize_exit_command', action = 'store_true', default = False,
                            help = 'adds a dummy no-op command before every exit function call')
    arg_parser.add_argument('-l', '--log',
                            dest = 'write_log_on_fail', action = 'store_true', default = False,
                            help = 'dumps the compiler output to a log file on failed compilation')
    arg_parser.add_argument('source_file', type = FileType('r', encoding = 'latin-1'))
    arg_parser.add_argument('output_file', nargs = '?')

    args = arg_parser.parse_args()

    # determine the base directory of the source file
    basepath = None

    if args.source_file.name != '<stdin>':
        basepath = os.path.dirname(os.path.abspath(args.source_file.name))

    # make sure that extra syntax checks are enabled if --optimize or --extra_branch_optimization arguments are used
    if (args.optimize or args.additional_branch_optimization) and args.extra_syntax_checks == False:
        args.extra_syntax_checks = True

    # read the source and compile it
    code = args.source_file.read()

    t1 = datetime.now()

    compiler = KSPCompiler(code,
                           basepath,
                           compact                        = args.compact,
                           combine_callbacks              = args.combine_callbacks,
                           compact_variables              = args.compact_variables,
                           extra_syntax_checks            = args.extra_syntax_checks,
                           optimize                       = args.optimize,
                           additional_branch_optimization = args.additional_branch_optimization,
                           sanitize_exit_command          = args.sanitize_exit_command,
                           add_compiled_date_comment      = args.add_compile_date,
                           force_compiler_arguments       = args.force_compiler_arguments,
                           write_log_on_fail              = args.write_log_on_fail,
                           compiled_code_tab_size         = args.num_spaces)

    compiler.compile(callback = utils.compile_on_progress)

    # write the compiled code to output
    code = compiler.compiled_code.replace('\r', '')
    paths = []
    out_is_dir = False

    if args.output_file:
        # we don't care about any paths from save_compiled_source pragmas in case we specified an output file argument
        compiler.output_files.clear()
        compiler.output_files.append(args.output_file)

    for p in compiler.output_files:
        path = p

        if not os.path.isabs(path):
            path = os.path.join(basepath, path)

        if os.path.isdir(path):
            out_is_dir = True
        else:
            with io.open(path, 'w', encoding = 'latin-1') as o:
                o.write(code)

            paths.append(path)

    delta = utils.calc_time_diff(datetime.now() - t1)

    if len(paths) > 0 and out_is_dir == False:
        utils.log_message("Successfully compiled in %s! Compiled code was saved to:" % delta)

        for p in paths:
            utils.log_message("    %s" % p)
    else:
        if out_is_dir:
            utils.log_message("The output path for the compiled code cannot be a folder, however compilation ended successfully in %s!" % delta)
        else:
            utils.log_message("The output file for the compiled code was not defined, however compilation ended successfully in %s!" % delta)

if __name__ == "__main__":
    main()