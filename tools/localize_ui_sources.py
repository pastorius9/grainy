"""One-time, explicit presentation-boundary migration; run on untranslated sources only."""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORDS = re.compile('[가-힣]')
ONE = {'QLabel', 'QPushButton', 'QCheckBox', 'QRadioButton', 'QGroupBox', 'QAction',
       'setText', 'setWindowTitle', 'setToolTip', 'setStatusTip', 'setPlaceholderText',
       'setAccessibleName', 'setSpecialValueText', 'setSuffix', 'setPrefix', 'setInformativeText',
       'addMenu', 'addAction', 'addItems', 'setHeaderLabels', 'showMessage'}
catalog = set()


def migrate(path):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines: starts.append(starts[-1] + len(line))
    def pos(line, col):
        return starts[line-1] + len(lines[line-1].encode('utf-8')[:col].decode('utf-8'))
    def span(node):
        return pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset)
    replacements = {}
    def visible(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and WORDS.search(node.value):
            catalog.add(node.value)
            replacements[span(node)] = f'tr({ast.get_source_segment(source,node)})'
        elif isinstance(node, ast.JoinedStr):
            template, values = '', []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    template += value.value.replace('{', '{{').replace('}', '}}')
                else:
                    template += '{'+str(len(values))+'}'
                    values.append(ast.unparse(ast.JoinedStr(values=[value])))
            if WORDS.search(template):
                catalog.add(template)
                replacements[span(node)] = 'tr('+repr(template)+(', '+', '.join(values) if values else '')+')'
        elif isinstance(node, ast.IfExp):
            visible(node.body);visible(node.orelse)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            visible(node.left);visible(node.right)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for item in node.elts:visible(item)
        # Never descend into data lookups, user names or arbitrary function results.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ''
        targets = []
        if name in ONE and node.args: targets = [node.args[0]]
        elif name in {'addRow','addItem'} and node.args:targets=[node.args[0]]
        elif name in {'setTabText','addTab'} and len(node.args)>1:targets=[node.args[1]]
        elif name == 'drawText' and node.args:targets=[node.args[-1]]
        elif name in {'information','warning','critical','question','getText','getItem','getInt','getDouble','getOpenFileName','getOpenFileNames','getSaveFileName','getExistingDirectory'}:
            targets=node.args[1:3]
        elif name in {'button','section','tab_page'} and node.args:targets=[node.args[0]]
        elif name in {'label','action','add_foldout'} and len(node.args)>1:targets=[node.args[1]]
        elif name == 'Adjustment' and len(node.args)>1:targets=[node.args[1]]
        elif name in {'add_adjustment','scalar'} and len(node.args)>2:targets=[node.args[2]]
        for target in targets:visible(target)
    # App-owned display helper arguments; do not wrap user-facing data widgets globally.
    safe = {
      'app.py': [('QLabel(text)','QLabel(tr(text))'),('QPushButton(text)','QPushButton(tr(text))'),('addTab(scroll,label)','addTab(scroll,tr(label))'),('toggle.setText(title)','toggle.setText(tr(title))'),('addItem(title,key)','addItem(tr(title),key)'),('addItem(label,ratio)','addItem(tr(label),ratio)')],
      'studio.py': [('QLabel(text)','QLabel(tr(text))'),('QPushButton(text)','QPushButton(tr(text))'),('QCheckBox(text)','QCheckBox(tr(text))'),('addItem(text,value)','addItem(tr(text),value)')],
      'manager.py': [('menu.addAction(title)','menu.addAction(tr(title))'),('QPushButton(title)','QPushButton(tr(title))'),('QCheckBox(title)','QCheckBox(tr(title))'),('setWindowTitle(title)','setWindowTitle(tr(title))'),('form.addRow(text,field)','form.addRow(tr(text),field)'),('addItem(text,value)','addItem(tr(text),value)')],
      'photo_actions.py': [('parent.addAction(text)','parent.addAction(tr(text))')],
      'widgets.py': [('QLabel(label)','QLabel(tr(label))')],
    }
    for (start,end),replacement in sorted(replacements.items(),reverse=True):source=source[:start]+replacement+source[end:]
    for before,after in safe.get(path.name,[]):source=source.replace(before,after)
    if replacements or path.name in safe:
        lines=source.splitlines(keepends=True)
        index=next((i+1 for i,line in enumerate(lines) if line.startswith('from __future__ import ')),0)
        # Keep module docstrings first.
        if not index and isinstance(tree.body[0],ast.Expr) and isinstance(tree.body[0].value,ast.Constant) and isinstance(tree.body[0].value.value,str):index=tree.body[0].end_lineno
        lines.insert(index,'from .i18n import tr\n')
        path.write_text(''.join(lines),encoding='utf-8')


if __name__=='__main__':
    for path in (ROOT/'luma').glob('*.py'):
        if path.name in {'i18n.py','selftest.py','language_dialog.py','desktop_session.py'}:continue
        if 'PySide6' in path.read_text(encoding='utf-8'):migrate(path)
    (ROOT/'validation'/'ui-translation-sources-0.5.36.json').write_text(json.dumps(sorted(catalog),ensure_ascii=False,indent=2),encoding='utf-8')
    print(len(catalog),'direct UI strings marked')
