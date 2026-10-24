"""What the window shows around the photo: full screen (F), the side panels (Tab), all panels (Shift+Tab) and lights out (L).
Esc leaves lights out, then full screen (MainWindow.keyPressEvent, after the crop tool and the other tools had their Esc)."""
from PySide6.QtGui import QBrush,QColor


class ScreenModes:
    def __init__(self,window):
        self.w=window;self.sides=self.bars=self.lights=False
        self.sizes=None;self.maximized=False        # the widths of the three columns before the sides went; whether full screen came from a maximized window

    def toggle_full_screen(self):
        w=self.w
        if w.isFullScreen():w.showMaximized() if self.maximized else w.showNormal()
        else:self.maximized=w.isMaximized();w.showFullScreen()

    def toggle_sides(self):
        self.sides=not self.sides;self.apply()

    def toggle_bars(self):
        """Shift+Tab: everything hidden, or everything shown again when the sides and the bars are both hidden already."""
        self.sides=self.bars=not(self.sides and self.bars);self.apply()

    def toggle_lights(self):
        w=self.w;self.lights=not self.lights;self.apply()
        if self.lights:
            black=QBrush(QColor('#000000'))
            for view in (w.view,w.compare.other):view.setBackgroundBrush(black)
            w.view.setFocus()
        else:w.set_view_background(w.catalog.preference('view_background','black'))

    def leave(self):
        """Esc: out of lights out, else out of full screen. False when it was neither, so Esc does what it does elsewhere."""
        if self.lights:self.toggle_lights()
        elif self.w.isFullScreen():self.toggle_full_screen()
        else:return False
        return True

    def apply(self):
        """Show what the modes leave: the two side columns, the header, the view bar, and the list below the photo or the
        toolbar of the library (whichever the screen has, see MainWindow.set_mode). Lights out also takes the menus and the status bar."""
        w=self.w;develop=w.stack.currentIndex()==0
        sides=not(self.sides or self.lights);bars=not(self.bars or self.lights)
        if not sides and not w.library_column.isHidden() and all(w.split.sizes()):self.sizes=w.split.sizes()
        restore=sides and w.library_column.isHidden()
        for widget,visible in ((w.library_column,sides),(w.edit_column,sides),(w.header_bar,bars),(w.view_bar,bars),
                (w.filmstrip,bars and develop),(w.library_toolbar,bars and not develop),(w.compare.bar,bars and w.compare.on),
                (w.menuBar(),not self.lights),(w.statusBar(),not self.lights)):
            widget.setVisible(visible)
        if restore and self.sizes:w.split.setSizes(self.sizes)
