import json
from threading import Event
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from luma import catalog_maintenance
from test_studio_ui import app,window,wait
from test_profile_store import settings


def test_optimizer_is_async_cancelable_and_retains_current_edit(window,monkeypatch):
    w=window;s=settings();ident=w.current_id
    w.catalog.set_settings(ident,s)
    w.settings=s;w.last_saved=s.copy();w.load_controls()
    # Make a legacy payload without changing the current in-memory adjustment.
    with w.catalog.db:w.catalog.db.execute('UPDATE photos SET edits=? WHERE id=?',(json.dumps(s),ident))
    wait(lambda:not w.jobs)
    started=Event();released=Event();native=catalog_maintenance.optimize;reports=[];ticks=[]
    def delayed(directory,cancel,progress):
        started.set()
        assert released.wait(10)
        return native(directory,cancel,progress)
    monkeypatch.setattr(catalog_maintenance,'optimize',delayed)
    monkeypatch.setattr(QMessageBox,'information',lambda *args:reports.append(args[2]))
    watch_active=w.extras.timer.isActive()
    ticker=QTimer(w);ticker.setInterval(10);ticker.timeout.connect(lambda:ticks.append(1));ticker.start()
    w.manager.optimize_catalog();dialog=w.manager.maintenance_dialog
    wait(lambda:started.is_set() and len(ticks)>4)
    assert w.maintenance_running and dialog.isVisible() and not w.extras.timer.isActive()
    w.close();assert w.isVisible() and w.maintenance_running
    dialog.reject();assert dialog.cancel.is_set() and dialog.isVisible()
    released.set();wait(lambda:not w.maintenance_running)
    assert w.manager.last_optimization['cancelled'] and reports
    assert w.settings==s and w.catalog.photo(ident)['settings']==s
    assert w.extras.timer.isActive()==watch_active
    monkeypatch.setattr(catalog_maintenance,'optimize',native)
    wait(lambda:not w.jobs);w.manager.optimize_catalog();wait(lambda:not w.maintenance_running)
    assert w.manager.last_optimization['converted']==1
    assert w.catalog.photo(ident)['settings']==s
    ticker.stop()
