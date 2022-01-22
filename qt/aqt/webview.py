# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import dataclasses
import json
import re
import sys
from typing import Any, Callable, Optional, Sequence, cast

import anki
from anki.lang import is_rtl
from anki.utils import is_lin, is_mac, is_win
from aqt import colors, gui_hooks
from aqt.qt import *
from aqt.theme import theme_manager
from aqt.utils import askUser, is_gesture_or_zoom_event, openLink, showInfo, tr

serverbaseurl = re.compile(r"^.+:\/\/[^\/]+")

# Page for debug messages
##########################################################################

BridgeCommandHandler = Callable[[str], Any]


class AnkiWebPage(QWebEnginePage):
    def __init__(self, onBridgeCmd: BridgeCommandHandler) -> None:
        QWebEnginePage.__init__(self)
        self._onBridgeCmd = onBridgeCmd
        self._setupBridge()
        self.open_links_externally = True

    def _setupBridge(self) -> None:
        class Bridge(QObject):
            def __init__(self, bridge_handler: Callable[[str], Any]) -> None:
                super().__init__()
                self.onCmd = bridge_handler

            @pyqtSlot(str, result=str)  # type: ignore
            def cmd(self, str: str) -> Any:
                return json.dumps(self.onCmd(str))

        self._bridge = Bridge(self._onCmd)

        self._channel = QWebChannel(self)
        self._channel.registerObject("py", self._bridge)
        self.setWebChannel(self._channel)

        qwebchannel = ":/qtwebchannel/qwebchannel.js"
        jsfile = QFile(qwebchannel)
        if not jsfile.open(QIODevice.OpenModeFlag.ReadOnly):
            print(f"Error opening '{qwebchannel}': {jsfile.error()}", file=sys.stderr)
        jstext = bytes(cast(bytes, jsfile.readAll())).decode("utf-8")
        jsfile.close()

        script = QWebEngineScript()
        script.setSourceCode(
            jstext
            + """
            var pycmd, bridgeCommand;
            new QWebChannel(qt.webChannelTransport, function(channel) {
                bridgeCommand = pycmd = function (arg, cb) {
                    var resultCB = function (res) {
                        // pass result back to user-provided callback
                        if (cb) {
                            cb(JSON.parse(res));
                        }
                    }
                
                    channel.objects.py.cmd(arg, resultCB);
                    return false;                   
                }
                pycmd("domDone");
            });
        """
        )
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setRunsOnSubFrames(False)
        self.profile().scripts().insert(script)

    def javaScriptConsoleMessage(
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        msg: str,
        line: int,
        srcID: str,
    ) -> None:
        # not translated because console usually not visible,
        # and may only accept ascii text
        if srcID.startswith("data"):
            srcID = ""
        else:
            srcID = serverbaseurl.sub("", srcID[:80], 1)
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel:
            level_str = "info"
        elif level == QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel:
            level_str = "warning"
        elif level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            level_str = "error"
        else:
            level_str = str(level)
        buf = "JS %(t)s %(f)s:%(a)d %(b)s" % dict(
            t=level_str, a=line, f=srcID, b=f"{msg}\n"
        )
        if "MathJax localStorage" in buf:
            # silence localStorage noise
            return
        elif "link preload" in buf:
            # silence 'link preload' warning on the first card
            return
        # ensure we don't try to write characters the terminal can't handle
        buf = buf.encode(sys.stdout.encoding, "backslashreplace").decode(
            sys.stdout.encoding
        )
        # output to stdout because it may raise error messages on the anki GUI
        # https://github.com/ankitects/anki/pull/560
        sys.stdout.write(buf)

    def acceptNavigationRequest(
        self, url: QUrl, navType: Any, isMainFrame: bool
    ) -> bool:
        if (
            not self.open_links_externally
            or "_anki/pages" in url.path()
            or url.path() == "/_anki/legacyPageData"
        ):
            return super().acceptNavigationRequest(url, navType, isMainFrame)

        if not isMainFrame:
            return True
        # data: links generated by setHtml()
        if url.scheme() == "data":
            return True
        # catch buggy <a href='#' onclick='func()'> links
        from aqt import mw

        if url.matches(
            QUrl(mw.serverURL()), cast(Any, QUrl.UrlFormattingOption.RemoveFragment)
        ):
            print("onclick handler needs to return false")
            return False
        # load all other links in browser
        openLink(url)
        return False

    def _onCmd(self, str: str) -> Any:
        return self._onBridgeCmd(str)

    def javaScriptAlert(self, frame: Any, text: str) -> None:
        showInfo(text)

    def javaScriptConfirm(self, frame: Any, text: str) -> bool:
        return askUser(text)


# Add-ons
##########################################################################


@dataclasses.dataclass
class WebContent:
    """Stores all dynamically modified content that a particular web view
    will be populated with.

    Attributes:
        body {str} -- HTML body
        head {str} -- HTML head
        css {List[str]} -- List of media server subpaths,
                           each pointing to a CSS file
        js {List[str]} -- List of media server subpaths,
                          each pointing to a JS file

    Important Notes:
        - When modifying the attributes specified above, please make sure your
        changes only perform the minimum requried edits to make your add-on work.
        You should avoid overwriting or interfering with existing data as much
        as possible, instead opting to append your own changes, e.g.:

            def on_webview_will_set_content(web_content: WebContent, context) -> None:
                web_content.body += "<my_html>"
                web_content.head += "<my_head>"

        - The paths specified in `css` and `js` need to be accessible by Anki's
          media server. All list members without a specified subpath are assumed
          to be located under `/_anki`, which is the media server subpath used
          for all web assets shipped with Anki.

          Add-ons may expose their own web assets by utilizing
          aqt.addons.AddonManager.setWebExports(). Web exports registered
          in this manner may then be accessed under the `/_addons` subpath.

          E.g., to allow access to a `my-addon.js` and `my-addon.css` residing
          in a "web" subfolder in your add-on package, first register the
          corresponding web export:

          > from aqt import mw
          > mw.addonManager.setWebExports(__name__, r"web/.*(css|js)")

          Then append the subpaths to the corresponding web_content fields
          within a function subscribing to gui_hooks.webview_will_set_content:

              def on_webview_will_set_content(web_content: WebContent, context) -> None:
                  addon_package = mw.addonManager.addonFromModule(__name__)
                  web_content.css.append(
                      f"/_addons/{addon_package}/web/my-addon.css")
                  web_content.js.append(
                      f"/_addons/{addon_package}/web/my-addon.js")

          Note that '/' will also match the os specific path separator.
    """

    body: str = ""
    head: str = ""
    css: list[str] = dataclasses.field(default_factory=lambda: [])
    js: list[str] = dataclasses.field(default_factory=lambda: [])


# Main web view
##########################################################################


class AnkiWebView(QWebEngineView):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        title: str = "default",
    ) -> None:
        QWebEngineView.__init__(self, parent=parent)
        self.set_title(title)
        self._page = AnkiWebPage(self._onBridgeCmd)
        # reduce flicker
        self._page.setBackgroundColor(
            self.get_window_bg_color(theme_manager.night_mode)
        )

        # in new code, use .set_bridge_command() instead of setting this directly
        self.onBridgeCmd: Callable[[str], Any] = self.defaultOnBridgeCmd

        self._domDone = True
        self._pendingActions: list[tuple[str, Sequence[Any]]] = []
        self.requiresCol = True
        self.setPage(self._page)
        self._disable_zoom = False

        self.resetHandlers()
        self._filterSet = False
        QShortcut(  # type: ignore
            QKeySequence("Esc"),
            self,
            context=Qt.ShortcutContext.WidgetWithChildrenShortcut,
            activated=self.onEsc,
        )
        gui_hooks.theme_did_change.append(self.on_theme_did_change)

    def set_title(self, title: str) -> None:
        self.title = title  # type: ignore[assignment]

    def disable_zoom(self) -> None:
        self._disable_zoom = True

    def eventFilter(self, obj: QObject, evt: QEvent) -> bool:
        if self._disable_zoom and is_gesture_or_zoom_event(evt):
            return True

        if (
            isinstance(evt, QMouseEvent)
            and evt.type() == QEvent.Type.MouseButtonRelease
        ):
            if evt.button() == Qt.MouseButton.MiddleButton and is_lin:
                self.onMiddleClickPaste()
                return True

        return False

    def set_open_links_externally(self, enable: bool) -> None:
        self._page.open_links_externally = enable

    def onEsc(self) -> None:
        w = self.parent()
        while w:
            if isinstance(w, QDialog) or isinstance(w, QMainWindow):
                from aqt import mw

                # esc in a child window closes the window
                if w != mw:
                    w.close()
                else:
                    # in the main window, removes focus from type in area
                    parent = self.parent()
                    assert isinstance(parent, QWidget)
                    parent.setFocus()
                break
            w = w.parent()

    def onCopy(self) -> None:
        self.triggerPageAction(QWebEnginePage.WebAction.Copy)

    def onCut(self) -> None:
        self.triggerPageAction(QWebEnginePage.WebAction.Cut)

    def onPaste(self) -> None:
        self.triggerPageAction(QWebEnginePage.WebAction.Paste)

    def onMiddleClickPaste(self) -> None:
        self.triggerPageAction(QWebEnginePage.WebAction.Paste)

    def onSelectAll(self) -> None:
        self.triggerPageAction(QWebEnginePage.WebAction.SelectAll)

    def contextMenuEvent(self, evt: QContextMenuEvent) -> None:
        m = QMenu(self)
        a = m.addAction(tr.actions_copy())
        qconnect(a.triggered, self.onCopy)
        gui_hooks.webview_will_show_context_menu(self, m)
        m.popup(QCursor.pos())

    def dropEvent(self, evt: QDropEvent) -> None:
        pass

    def setHtml(self, html: str) -> None:  #  type: ignore
        # discard any previous pending actions
        self._pendingActions = []
        self._domDone = True
        self._queueAction("setHtml", html)
        self.set_open_links_externally(True)
        self.show()

    def _setHtml(self, html: str) -> None:
        """Send page data to media server, then surf to it.

        This function used to be implemented by QWebEngine's
        .setHtml() call. It is no longer used, as it has a
        maximum size limit, and due to security changes, it
        will stop working in the future."""
        from aqt import mw

        oldFocus = mw.app.focusWidget()
        self._domDone = False

        webview_id = id(self)
        mw.mediaServer.set_page_html(webview_id, html)
        self.load_url(QUrl(f"{mw.serverURL()}_anki/legacyPageData?id={webview_id}"))

        # work around webengine stealing focus on setHtml()
        # fixme: check which if any qt versions this is still required on
        if oldFocus:
            oldFocus.setFocus()

    def load_url(self, url: QUrl) -> None:
        # allow queuing actions when loading url directly
        self._domDone = False
        super().load(url)

    def zoomFactor(self) -> float:
        # overridden scale factor?
        webscale = os.environ.get("ANKI_WEBSCALE")
        if webscale:
            return float(webscale)

        if qtmajor > 5 or is_mac:
            return 1
        screen = QApplication.desktop().screen()  # type: ignore
        if screen is None:
            return 1

        dpi = screen.logicalDpiX()
        factor = dpi / 96.0
        if is_lin:
            factor = max(1, factor)
            return factor
        return 1

    def setPlaybackRequiresGesture(self, value: bool) -> None:
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, value
        )

    def _getQtIntScale(self, screen: QWidget) -> int:
        # try to detect if Qt has scaled the screen
        # - qt will round the scale factor to a whole number, so a dpi of 125% = 1x,
        #   and a dpi of 150% = 2x
        # - a screen with a normal physical dpi of 72 will have a dpi of 32
        #   if the scale factor has been rounded to 2x
        # - different screens have different physical DPIs (eg 72, 93, 102)
        # - until a better solution presents itself, assume a physical DPI at
        #   or above 70 is unscaled
        if screen.physicalDpiX() > 70:
            return 1
        elif screen.physicalDpiX() > 35:
            return 2
        else:
            return 3

    def get_window_bg_color(self, night_mode: bool) -> QColor:
        if night_mode:
            return QColor(colors.WINDOW_BG[1])
        elif is_mac:
            # standard palette does not return correct window color on macOS
            return QColor("#ececec")
        else:
            return theme_manager.default_palette.color(QPalette.ColorRole.Window)

    def standard_css(self) -> str:
        palette = theme_manager.default_palette
        color_hl = palette.color(QPalette.ColorRole.Highlight).name()

        if is_win:
            # T: include a font for your language on Windows, eg: "Segoe UI", "MS Mincho"
            family = tr.qt_misc_segoe_ui()
            button_style = "button { font-family:%s; }" % family
            button_style += "\n:focus { outline: 1px solid %s; }" % color_hl
            font = f"font-size:12px;font-family:{family};"
        elif is_mac:
            family = "Helvetica"
            font = f'font-size:15px;font-family:"{family}";'
            button_style = """
button { -webkit-appearance: none; background: #fff; border: 1px solid #ccc;
border-radius:5px; font-family: Helvetica }"""
        else:
            family = self.font().family()
            color_hl_txt = palette.color(QPalette.ColorRole.HighlightedText).name()
            color_btn = palette.color(QPalette.ColorRole.Button).name()
            font = f'font-size:14px;font-family:"{family}", sans-serif;'
            button_style = """
/* Buttons */
button{{ 
        background-color: {color_btn};
        font-family:"{family}", sans-serif; }}
button:focus{{ border-color: {color_hl} }}
button:active, button:active:hover {{ background-color: {color_hl}; color: {color_hl_txt};}}
/* Input field focus outline */
textarea:focus, input:focus, input[type]:focus, .uneditable-input:focus,
div[contenteditable="true"]:focus {{   
    outline: 0 none;
    border-color: {color_hl};
}}""".format(
                family=family,
                color_btn=color_btn,
                color_hl=color_hl,
                color_hl_txt=color_hl_txt,
            )

        zoom = self.zoomFactor()

        window_bg_day = self.get_window_bg_color(False).name()
        window_bg_night = self.get_window_bg_color(True).name()

        return f"""
body {{ zoom: {zoom}; background-color: var(--window-bg); }}
html {{ {font} }}
{button_style}
:root {{ --window-bg: {window_bg_day} }}
:root[class*=night-mode] {{ --window-bg: {window_bg_night} }}
"""

    def stdHtml(
        self,
        body: str,
        css: Optional[list[str]] = None,
        js: Optional[list[str]] = None,
        head: str = "",
        context: Optional[Any] = None,
        default_css: bool = True,
    ) -> None:
        css = (["css/webview.css"] if default_css else []) + (
            [] if css is None else css
        )
        web_content = WebContent(
            body=body,
            head=head,
            js=["js/webview.js"] + (["js/vendor/jquery.min.js"] if js is None else js),
            css=css,
        )

        gui_hooks.webview_will_set_content(web_content, context)

        csstxt = ""
        if "css/webview.css" in css:
            # we want our dynamic styling to override the defaults in
            # css/webview.css, but come before user-provided stylesheets so that
            # they can override us if necessary
            web_content.css.remove("css/webview.css")
            csstxt = self.bundledCSS("css/webview.css")
            csstxt += f"<style>{self.standard_css()}</style>"

        csstxt += "\n".join(self.bundledCSS(fname) for fname in web_content.css)
        jstxt = "\n".join(self.bundledScript(fname) for fname in web_content.js)

        from aqt import mw

        head = mw.baseHTML() + csstxt + jstxt + web_content.head
        body_class = theme_manager.body_class()

        if theme_manager.night_mode:
            doc_class = "night-mode"
        else:
            doc_class = ""

        if is_rtl(anki.lang.current_lang):
            lang_dir = "rtl"
        else:
            lang_dir = "ltr"

        html = f"""
<!doctype html>
<html class="{doc_class}" dir="{lang_dir}">
<head>
    <title>{self.title}</title>
{head}
</head>

<body class="{body_class}">{web_content.body}</body>
</html>"""
        # print(html)
        self.setHtml(html)

    @classmethod
    def webBundlePath(cls, path: str) -> str:
        from aqt import mw

        if path.startswith("/"):
            subpath = ""
        else:
            subpath = "/_anki/"

        return f"http://127.0.0.1:{mw.mediaServer.getPort()}{subpath}{path}"

    def bundledScript(self, fname: str) -> str:
        return f'<script src="{self.webBundlePath(fname)}"></script>'

    def bundledCSS(self, fname: str) -> str:
        return '<link rel="stylesheet" type="text/css" href="%s">' % self.webBundlePath(
            fname
        )

    def eval(self, js: str) -> None:
        self.evalWithCallback(js, None)

    def evalWithCallback(self, js: str, cb: Callable) -> None:
        self._queueAction("eval", js, cb)

    def _evalWithCallback(self, js: str, cb: Callable[[Any], Any]) -> None:
        if cb:

            def handler(val: Any) -> None:
                if self._shouldIgnoreWebEvent():
                    print("ignored late js callback", cb)
                    return
                cb(val)

            self.page().runJavaScript(js, handler)
        else:
            self.page().runJavaScript(js)

    def _queueAction(self, name: str, *args: Any) -> None:
        self._pendingActions.append((name, args))
        self._maybeRunActions()

    def _maybeRunActions(self) -> None:
        if sip.isdeleted(self):
            return
        while self._pendingActions and self._domDone:
            name, args = self._pendingActions.pop(0)

            if name == "eval":
                self._evalWithCallback(*args)
            elif name == "setHtml":
                self._setHtml(*args)
            else:
                raise Exception(f"unknown action: {name}")

    def _openLinksExternally(self, url: str) -> None:
        openLink(url)

    def _shouldIgnoreWebEvent(self) -> bool:
        # async web events may be received after the profile has been closed
        # or the underlying webview has been deleted
        from aqt import mw

        if sip.isdeleted(self):
            return True
        if not mw.col and self.requiresCol:
            return True
        return False

    def _onBridgeCmd(self, cmd: str) -> Any:
        if self._shouldIgnoreWebEvent():
            print("ignored late bridge cmd", cmd)
            return

        if not self._filterSet:
            self.focusProxy().installEventFilter(self)
            self._filterSet = True

        if cmd == "domDone":
            self._domDone = True
            self._maybeRunActions()
        else:
            handled, result = gui_hooks.webview_did_receive_js_message(
                (False, None), cmd, self._bridge_context
            )
            if handled:
                return result
            else:
                return self.onBridgeCmd(cmd)

    def defaultOnBridgeCmd(self, cmd: str) -> None:
        print("unhandled bridge cmd:", cmd)

    # legacy
    def resetHandlers(self) -> None:
        self.onBridgeCmd = self.defaultOnBridgeCmd
        self._bridge_context = None

    def adjustHeightToFit(self) -> None:
        self.evalWithCallback("document.documentElement.offsetHeight", self._onHeight)

    def _onHeight(self, qvar: Optional[int]) -> None:
        from aqt import mw

        if qvar is None:

            mw.progress.timer(1000, mw.reset, False)
            return

        self.setFixedHeight(int(qvar))

    def set_bridge_command(self, func: Callable[[str], Any], context: Any) -> None:
        """Set a handler for pycmd() messages received from Javascript.

        Context is the object calling this routine, eg an instance of
        aqt.reviewer.Reviewer or aqt.deckbrowser.DeckBrowser."""
        self.onBridgeCmd = func
        self._bridge_context = context

    def hide_while_preserving_layout(self) -> None:
        "Hide but keep existing size."
        sp = self.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.setSizePolicy(sp)
        self.hide()

    def inject_dynamic_style_and_show(self) -> None:
        "Add dynamic styling, and reveal."
        css = self.standard_css()

        def after_style(arg: Any) -> None:
            gui_hooks.webview_did_inject_style_into_page(self)
            self.show()

        self.evalWithCallback(
            f"""
const style = document.createElement('style');
style.innerHTML = `{css}`;
document.head.appendChild(style);
""",
            after_style,
        )

    def load_ts_page(self, name: str) -> None:
        from aqt import mw

        self.set_open_links_externally(True)
        if theme_manager.night_mode:
            extra = "#night"
        else:
            extra = ""
        self.hide_while_preserving_layout()
        self.load_url(QUrl(f"{mw.serverURL()}_anki/pages/{name}.html{extra}"))
        self.inject_dynamic_style_and_show()

    def force_load_hack(self) -> None:
        """Force process to initialize.
        Must be done on Windows prior to changing current working directory."""
        self.requiresCol = False
        self._domReady = False
        self._page.setContent(cast(QByteArray, bytes("", "ascii")))

    def cleanup(self) -> None:
        try:
            from aqt import mw
        except ImportError:
            # this will fail when __del__ is called during app shutdown
            return

        gui_hooks.theme_did_change.remove(self.on_theme_did_change)
        mw.mediaServer.clear_page_html(id(self))

    def on_theme_did_change(self) -> None:
        # avoid flashes if page reloaded
        self._page.setBackgroundColor(
            self.get_window_bg_color(theme_manager.night_mode)
        )
        # update night-mode class, and legacy nightMode/night-mode body classes
        self.eval(
            f"""
(function() {{
    const doc = document.documentElement.classList;
    const body = document.body.classList;
    if ({1 if theme_manager.night_mode else 0}) {{
        doc.add("night-mode");
        body.add("night-mode");
        body.add("nightMode");
    }} else {{
        doc.remove("night-mode");
        body.remove("night-mode");
        body.remove("nightMode");
    }}
}})();
"""
        )
