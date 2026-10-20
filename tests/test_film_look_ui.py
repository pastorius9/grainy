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
