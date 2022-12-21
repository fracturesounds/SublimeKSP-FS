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
import os
import re

class KSPBuiltins:
	def __init__(self, target_version):
		self.target_version = target_version
		self.base_path = os.path.dirname(os.path.abspath(__file__))
		self.variables = set()
		self.functions = set()
		self.preprocessor_functions = set()
		self.keywords = set()
		self.control_parameters = set()
		self.string_typed_control_parameters = set()
		self.engine_parameters = set()
		self.event_parameters = set()
		self.function_signatures = {}
		self.functions_with_forced_parentheses = set()
		self.functions_with_constant_return = set() # Functions with return values that can be used for const variables

		self.data = {
		'variables'							: self.variables,
		'functions'							: self.functions,
		'preprocessor_functions'			: self.preprocessor_functions,
		'keywords' 							: self.keywords,
		'control_parameters'				: self.control_parameters,
		'string_typed_control_parameters'	: self.string_typed_control_parameters,
		'engine_parameters'					: self.engine_parameters,
		'event_parameters' 					: self.event_parameters,
		'functions_with_forced_parentheses'	: self.functions_with_forced_parentheses,
		'functions_with_constant_return'	: self.functions_with_constant_return,
		}

		self.get_data(target_version)

	def get_data(self, target_version):
		section = None
		builtins_filepath = "%s/builtins_data/K%s.txt" % (self.base_path, target_version)
		builtins_data = open(builtins_filepath, 'r+', encoding='utf-8').read()
		lines = builtins_data.replace('\r\n', '\n').split('\n')

		for line in lines:
			line = line.strip()
			if line.startswith('['):
				section = line[1:-1].strip()
			elif line and section:
				self.data[section].add(line)

				if section == 'functions':
					m = re.match(r'(?P<name>\w+)(\((?P<params>.*?)\))?(:(?P<return_type>\w+))?', line)
					name, params, return_type = m.group('name'), m.group('params'), m.group('return_type')
					params = [p.strip() for p in params.replace('<', '').replace('>', '').split(',') if p.strip()]
					self.function_signatures[name] = (params, return_type)

				if section == 'preprocessor_functions':
					m = re.match(r'(?P<name>\w+)(\((?P<params>.*?)\))?(:(?P<return_type>\w+))?', line)
					name, params, return_type = m.group('name'), m.group('params'), m.group('return_type')
					params = [p.strip() for p in params.replace('<', '').replace('>', '').split(',') if p.strip()]
					self.function_signatures[name] = (params, return_type)
					self.data['functions'].add(line) # add preprocessor_functions to functions

				if section == 'variables':
					m = re.match(r'(?P<control_par>\$CONTROL_PAR_\w+?)|(?P<engine_par>\$ENGINE_PAR_\w+?)|(?P<event_par>\$EVENT_PAR_\w+?)', line)
					if m:
						control_par, engine_par, event_par = m.group('control_par'), m.group('engine_par'), m.group('event_par')
						if control_par:
							self.control_parameters.add(line)
						elif engine_par:
							self.engine_parameters.add(line)
						elif event_par:
							self.event_parameters.add(line)

		# mapping from function_name to descriptive string
		self.functions = dict([(x.split('(')[0], x) for x in self.functions])
		self.variables_unprefixed = set([v[1:] for v in self.variables])
		self.keywords = set(self.keywords).union(set(['async_complete']))

if __name__ == '__main__':
	builtins_object = KSPBuiltins(5.5)
