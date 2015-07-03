#!/usr/bin/env python
from __future__ import division, print_function

from collections import OrderedDict
import itertools
import json
import os.path
import re
import sys

from pyparsing import (LineEnd, Literal, Optional, ParseException, Word,
                       alphanums, hexnums, nums)
import pydot


def _parser():
    # Program definition
    #   P=(HEX ID=780 N=(python) T=0.008000)
    program = (Literal('P=(HEX ID=') + Word(hexnums) + Literal('N=(') +
               Word(alphanums).setResultsName('program_name') +
               Literal(') T=') + Word(nums + '.').setResultsName('interval') +
               Literal(')') + LineEnd())

    # Engine name
    #   E1=python
    #   E1
    engine_def = Literal('=') + Word(alphanums).setResultsName('engine_name')
    engine = (Literal('E') + Word(hexnums).setResultsName('engine_num') +
              Optional(engine_def))

    # Function name (may be anonymous)
    #   N=(Py_InitializeEx)
    #   N=(@?0x7f1b9e6b1d73)
    anonymous = Literal('@?0x') + Word(hexnums).setResultsName('anonymous')
    named = Word(alphanums + '_').setResultsName('func_name')
    name = Literal('N=(') + (anonymous | named) + Literal(')')

    # File position (via definition or reference)
    #   F0=(python)+648
    #   F2+10f9ac
    path = Word(alphanums + '/-_.').setResultsName('file_path')
    file_def = Literal('=(') + path + Literal(')')
    file_offset = Literal('+') + Word(hexnums).setResultsName('file_offset')
    file_pos = (Literal('F') + Word(hexnums).setResultsName('file_num') +
                Optional(file_def) + file_offset)

    # Function
    #   FN0=(F0=(python)+648 N=(@?0x400648))+0
    #   FN7+52
    func_def = Literal('=(') + file_pos + name + Literal(')')
    func_offset = Literal('+') + Word(hexnums).setResultsName('func_offset')
    function = (Literal('FN') + Word(hexnums).setResultsName('func_num') +
                Optional(func_def) + func_offset)

    # Value
    #   V0=(PERF_TICKS):(3d,3d,3d)
    #   V0:(4,4,4)
    value = (Literal('V0') + Optional(Literal('=(PERF_TICKS)')) +
             Literal(':(') + Word(hexnums).setResultsName('ticks') +
             Literal(',') + Word(hexnums) + Literal(',') + Word(hexnums) +
             Literal(')'))

    # Call frame
    # NB. Ignores counter definitions/references
    #   Cf FN9+1ef
    #   C14 FN9=(F1+17715 N=(@?0x804c533))+0 V0=(PERF_TICKS):(1,1,1)
    #   C14 FN9=(F1+17715 N=(@?0x804c533))+0 V0:(4,4,4)
    #   C14 E1=python FN9=(F1+58 N=(basename))+2
    call_frame = (Literal('C') + Word(hexnums).setResultsName('call_num') +
                  Optional(engine) + function + Optional(value) + LineEnd())

    item = program | call_frame
    return item


def _update_tree(root, stack):
    ticks = stack[-1]['ticks']
    root['count'] += ticks
    node = root
    for call in stack[1:]:
        call_id = ':'.join((call['file'], call['function'], call['offset']))
        children = node['children']
        if call_id in children:
            node = children[call_id]
            node['count'] += ticks
        else:
            node = call
            node['children'] = OrderedDict()
            node['count'] = ticks
            children[call_id] = node


def _tree_from_igprof(filename):
    with open(filename) as f:
        item = _parser()

        # A mapping from engine number to engine name.
        engines = {}

        # A mapping from file number to filename.
        files = {}

        # A mapping from function number to file and name.
        functions = {}

        root = {'type': 'ROOT', 'count': 0, 'children': OrderedDict()}
        last_call_num = 0
        stack = [{'type': 'ROOT'}]
        for line_num, line in enumerate(f):
            try:
                result = item.parseString(line)
            except ParseException as e:
                print(e)
                print(e.line)
                print(' ' * e.loc + '^')
                exit()
            if result[0] == 'C':
                engine = engines.setdefault(result.engine_num,
                                            result.engine_name)

                if result.func_num not in functions:
                    if result.file_num not in files:
                        files[result.file_num] = result.file_path
                    functions[result.func_num] = {
                        'file': files[result.file_num],
                        'function': (result.func_name or result.anonymous)}
                function = functions[result.func_num]

                call = {'type': 'C', 'offset': result.func_offset,
                        'engine': engine}
                call.update(function)
                if result.ticks:
                    call['ticks'] = int(result.ticks, 16)
                call_num = int(result.call_num, 16)
                if call_num <= last_call_num:
                    # Blend stack into tree
                    _update_tree(root, stack)

                    # Truncate the stack to the last common ancestor.
                    stack = stack[:call_num]
                stack.append(call)
                last_call_num = call_num
            elif line[0] == 'P':
                root['name'] = result.program_name
                root['interval'] = float(result.interval)
        _update_tree(root, stack)

    # Convert 'children' from OrderedDict to list.
    def _convert_children(node):
        children = list(node['children'].itervalues())
        node['children'] = children
        for node in children:
            _convert_children(node)

    _convert_children(root)

    return root


_NODE_ID_ITERATOR = itertools.count()
_MAX = 1


def _root_node(node, node_id, style, max_count):
    label = '$ {}\n{} : 100%'.format(node['name'], node['count'])
    style['shape'] = 'doublecircle'
    return pydot.Node(node_id, label=label, **style)


def _c_node(node, node_id, style, max_count):
    label = '{} @ {}\n{} : {:.3g}%'.format(node['function'],
                                           node['offset'],
                                           node['count'],
                                           node['count'] / _MAX * 100)
    return pydot.Node(node_id, label=label, **style)


def _python_node(node, node_id, style, max_count):
    offset = int(node['offset'], 16)
    label = '{}, line {}\n{} : {:.3g}%'.format(node['file'], offset,
                                           node['count'],
                                           node['count'] / _MAX * 100)
    style['shape'] = 'box'
    return pydot.Node(node_id, label=label, **style)


_ENGINE_FORMATTERS = {
    'ROOT': _root_node,
    '': _c_node,
    'python': _python_node
}


def _dump(graph, node, parent_id):
    node_id = next(_NODE_ID_ITERATOR)
    global _MAX
    if node_id == 0:
        _MAX = node['count']
    hue = 0.5 - (node['count'] / _MAX) / 2
    style = {'fillcolor': str(hue) + ' 1.0 1.0', 'style': 'filled'}
    engine = node.get('engine', 'ROOT')
    formatter = _ENGINE_FORMATTERS[engine]
    graph.add_node(formatter(node, node_id, style, _MAX))
    if engine != 'ROOT':
        graph.add_edge(pydot.Edge(parent_id, node_id))
    for child in node['children']:
        _dump(graph, child, node_id)


def from_igprof(filename):
    print('Converting to tree')
    root = _tree_from_igprof(filename)

    print('Creating graphviz graph')
    graph = pydot.Dot(graph_type='digraph')
    _dump(graph, root, 0)

    print('Drawing PNG')
    graph_path = os.path.join(os.path.dirname(filename), 'graph.png')
    graph.write_png(graph_path)


if __name__ == '__main__':
    graph = from_igprof(sys.argv[1])
