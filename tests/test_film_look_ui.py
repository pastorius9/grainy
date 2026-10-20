import os

from test_studio_ui import app, window, wait
from luma import film_look


def test_choosing_a_film_sets_its_grain_and_none_takes_it_back(window):
    w = window
    studio = w.studio
    combo = studio.film_choice
    first = film_look.catalog()[0]
    assert combo.itemData(0) == '' and not w.adjustments['film_strength'].isEnabled()
    history = len(w.catalog.histories(w.current_id))
    combo.setCurrentIndex(combo.findData(first['id']))
    wait(lambda: w.settings['film_look'] == first['id'])
    assert w.adjustments['film_strength'].isEnabled() and first['name'] not in studio.film_note.text()
    assert 'ISO' in studio.film_note.text() or not first.get('iso')
    if first.get('grain'):
        assert all(abs(w.settings[k] - v) < 1e-6 for k, v in first['grain'].items())
    assert len(w.catalog.histories(w.current_id)) == history + 1           # one undo step
    combo.setCurrentIndex(0)
    wait(lambda: w.settings['film_look'] == '')
    assert w.settings['grain'] == 0 and not w.adjustments['film_strength'].isEnabled()
    if os.environ.get('GRAINY_FILM_SCREENSHOT'):
        combo.setCurrentIndex(combo.findData(first['id']))
        w.tabs.sections['film'].set_expanded(True)
        w.tabs.sections['film'].grab().save(os.environ['GRAINY_FILM_SCREENSHOT'])


def test_film_list_groups_the_catalog(window):
    combo = window.studio.film_choice
    ids = [combo.itemData(i) for i in range(combo.count()) if combo.itemData(i)]
    assert ids == [e['id'] for e in film_look.catalog()]
    headers = [combo.itemText(i) for i in range(combo.count()) if combo.itemData(i) is None and combo.itemText(i)]
    assert headers and len(headers) == len({e['group'] for e in film_look.catalog()})


def test_presets_panel_has_a_folder_per_maker_and_one_for_other_presets(window):
    from PySide6.QtCore import Qt
    w = window
    tree = w.preset_tree
    tops = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    makers = [maker for maker, _ in film_look.by_maker()]
    assert [t.data(0, Qt.ItemDataRole.UserRole) for t in tops] == [('folder', m) for m in makers] + [('folder', 'others')]
    assert makers[:3] == ['Kodak', 'Fujifilm', 'Ilford'] and not any(t.isExpanded() for t in tops)

    def leaves(item):
        kind = item.data(0, Qt.ItemDataRole.UserRole)
        return [kind] if kind[0] != 'folder' else [k for i in range(item.childCount()) for k in leaves(item.child(i))]
    films = [k[1] for t in tops[:-1] for k in leaves(t)]
    assert sorted(films) == sorted(e['id'] for e in film_look.catalog())
    assert [k[1] for k in leaves(tops[-1])] == list(w.all_presets)
    kodak = tops[0]
    assert kodak.childCount() > 1 and all(kodak.child(i).data(0, Qt.ItemDataRole.UserRole)[0] == 'folder' for i in range(kodak.childCount()))
    # A click on a folder opens it; on a film, chooses it as the Film panel does; on a preset, applies it.
    w.preset_clicked(kodak)
    assert kodak.isExpanded()
    first = kodak.child(0).child(0)
    assert first.text(0) == film_look.short_name(film_look.entry(first.data(0, Qt.ItemDataRole.UserRole)[1]))
    w.preset_clicked(first)
    wait(lambda: w.settings['film_look'] == first.data(0, Qt.ItemDataRole.UserRole)[1])
    assert w.studio.film_choice.currentData() == w.settings['film_look']
    mono = next(k for k in leaves(tops[-1]) if w.all_presets[k[1]].get('monochrome'))
    w.apply_preset(mono[1])
    wait(lambda: w.settings['monochrome'])
    w.refresh_presets()
    assert w.preset_tree.topLevelItem(0).isExpanded()                     # a refresh keeps folders as they were
    if os.environ.get('GRAINY_FILM_SCREENSHOT'):
        w.preset_tree.grab().save(os.environ['GRAINY_FILM_SCREENSHOT'].replace('.png', '-presets.png'))
