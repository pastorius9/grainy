from test_studio_ui import app,wait


def test_wait_rechecks_condition_that_changes_during_loop_exit(app):
    # Model idle observed by the poll, then a same-batch paint scheduling work.
    observations=iter([False,True,False,True,True,True])
    wait(lambda:next(observations,True),timeout=100)
