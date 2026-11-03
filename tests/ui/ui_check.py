# SPDX-License-Identifier: MIT

"""End-to-end UI check in headless Firefox via WebDriver (geckodriver), against the simulated PLC.

    AB_CONNECT_FAKE=1 python3 -m app.main            # with an empty data/ folder
    geckodriver --port 4444                          # Ubuntu's Firefox snap: /snap/bin/firefox.geckodriver
    python3 tests/ui/ui_check.py <screenshot folder>  # with the Firefox snap: a folder under ~/snap/firefox/common
"""

import base64
import json
import sys
import time
import urllib.request

WD = "http://127.0.0.1:4444"
URL = "http://127.0.0.1:12348/ab-connect/?token=dev"
OUT = sys.argv[1] if len(sys.argv) > 1 else None


def call(method, path, body=None):
    req = urllib.request.Request(WD + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as r:
        return json.loads(r.read())["value"]


sid = call("POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "firefox",
          "moz:firefoxOptions": {"args": ["-headless", "--width=1500", "--height=920"]}}}})["sessionId"]
S = f"/session/{sid}"


def js(script):
    return call("POST", S + "/execute/sync", {"script": script, "args": []})


def until(expr, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if js("return (" + expr + ")"):
            return
        time.sleep(0.2)
    raise SystemExit("timeout waiting for: " + expr)


def shot(name):
    if OUT:
        with open(f"{OUT}/{name}.png", "wb") as f:
            f.write(base64.b64decode(call("GET", S + "/screenshot")))


def node(name, nth=0):
    return f"[...document.querySelectorAll('.node')].filter(n => n.querySelector('.name').textContent === {json.dumps(name)})[{nth}]"


def has_node(name):
    return f"[...document.querySelectorAll('.node .name')].some(n => n.textContent === {json.dumps(name)})"


try:
    call("POST", S + "/url", {"url": URL})
    time.sleep(1)
    js("window.__errors = []; window.addEventListener('error', e => window.__errors.push(String(e.message)));"
       "window.addEventListener('unhandledrejection', e => window.__errors.push('promise: ' + String(e.reason)));")
    js("document.getElementById('add-controller').click()")
    js("document.getElementById('cf-name').value = 'Line1'; document.getElementById('cf-address').value = '192.168.1.21';")
    js("document.querySelector('#controller-form button[type=submit]').click()")
    until(has_node("MainProgram"))
    print("scopes:", js("return [...document.querySelectorAll('.node .name')].map(n => n.textContent)"))

    for name, child in (("Controller tags", "Station1"), ("Station1", "Pos"), ("Stations", "[3]"),
                        ("MainProgram", "arBig")):
        js(node(name) + ".querySelector('.toggle').click()")
        until(has_node(child))
    js(node("[2]") + ".querySelector('.toggle').click()")
    until(f"[...document.querySelectorAll('.node .name')].filter(n => n.textContent === 'Axis').length >= 2")
    js(node("arBig") + ".querySelector('.toggle').click()")
    until("document.querySelectorAll('.more button').length >= 1")
    print("paging:", js("return document.querySelector('.more button').textContent"))

    js(node("Pos") + ".querySelector('.actions button').click()")           # preview
    until("document.querySelector('#preview pre') !== null")
    print("preview:", js("return document.getElementById('preview').innerText.replace(/\\n/g, ' | ')"))

    js("document.getElementById('opt-rate').value = 300")
    js(node("Pos") + ".querySelectorAll('.actions button')[1].click()")     # add one array
    until("document.getElementById('selected-count').textContent === '(1)'")
    js(node("Station1") + ".querySelectorAll('.actions button')[1].click()")  # add a whole UDT
    until("document.getElementById('selected-count').textContent === '(9)'")
    print("marked as selected:", js("return [...document.querySelectorAll('.node')].filter(n => n.querySelector('.added')).map(n => n.querySelector('.name').textContent)"))
    shot("1-browse")

    js("document.querySelector('.tab[data-view=selected]').click()")
    time.sleep(1.5)
    for r in js("return [...document.querySelectorAll('#tag-table tbody tr')].slice(0, 3).map(r => [...r.cells].slice(1).map(c => { "
                "const i = c.querySelector('select,input'); return i ? (i.type === 'checkbox' ? i.checked : i.value) : c.textContent; }))"):
        print("row:", r)
    row = "document.querySelector('#tag-table tr[data-node=\"controller/Station1/Status\"]')"
    js(f"const s = {row}.querySelector('select'); s.value = 'on-request'; s.dispatchEvent(new Event('change'));")
    time.sleep(1)
    js(f"const c = {row}.cells[6].querySelector('input'); c.checked = true; c.dispatchEvent(new Event('change'));")
    time.sleep(1.5)
    print("Status row:", js(f"const r = {row}; return [r.querySelector('select').value, r.cells[6].querySelector('input').checked, r.lastChild.textContent]"))
    print("scan groups:", js("return [...document.querySelectorAll('#cd-groups tr')].map(r => r.innerText.replace(/\\t/g, ' '))"))
    # the 1 s refresh must not rebuild the table (that closed open drop-downs)
    js("window.__sel = document.querySelector('#tag-table tr[data-node=\"controller/Station1/Name\"] select')")
    time.sleep(2.5)
    print("drop-down survives refreshes:", js("return document.contains(window.__sel)"),
          "| values still updating:", js("return document.querySelector('#tag-table tr[data-node=\"controller/Station1/Axis/ActPos\"]').lastChild.textContent"))
    shot("2-selected")
    # buttons line up: same height per size in the add-controller form
    js("document.getElementById('add-controller').click()")
    time.sleep(0.3)
    print("form button heights:", js("return [...document.querySelectorAll('#controller-form button')].map(b => b.textContent.trim() + ' ' + b.offsetHeight)"))
    shot("3-form")
    # the controller pane can be resized by dragging the handle (clamped to 200-600 px)
    width = "document.querySelector('.sidebar').getBoundingClientRect().width"
    drag = ("const h = document.getElementById('resizer'), r = h.getBoundingClientRect(), x = r.left + 3;"
            "const ev = (t, cx) => h.dispatchEvent(new PointerEvent(t, {bubbles: true, pointerId: 1, clientX: cx, clientY: 300}));"
            "ev('pointerdown', x); ev('pointermove', x + {dx}); ev('pointerup', x + {dx});")
    before = js("return " + width)
    js(drag.replace("{dx}", "150"))
    wider = js("return " + width)
    js(drag.replace("{dx}", "2000"))
    print("pane widths:", round(before), "-> dragged +150:", round(wider), "-> clamped:", round(js("return " + width)),
          "| saved:", js("return localStorage.getItem('ab-connect-sidebar-width')"))
    shot("4-resized")
    js("document.getElementById('resizer').dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
    print("after double-click:", round(js("return " + width)))
    print("page errors:", js("return window.__errors"))
finally:
    call("DELETE", S)
