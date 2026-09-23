"""Small Windows control window for Pair Desk.

The title-bar close button only minimizes. 「退出交付台」 is the intentional
stop. Minimizing leaves the HTTP worker running. No extra GUI dependency:
tkinter ships with the Anaconda/official CPython Windows builds.
"""
from __future__ import annotations

import webbrowser

from .desk_service import DEFAULT_PORT, desk_url, is_running, start_desk, stop_desk


def run_app(port: int = DEFAULT_PORT, home=None) -> int:
    import tkinter as tk
    from tkinter import messagebox

    url = desk_url(port)
    root = tk.Tk()
    root.title("Pair 交付台")
    root.geometry("380x200")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", False)
    except tk.TclError:
        pass

    status = tk.StringVar(value="正在连接…")
    tk.Label(root, text="Pair 交付台", font=("Segoe UI", 16, "bold")).pack(pady=(16, 4))
    tk.Label(root, textvariable=status, font=("Segoe UI", 10)).pack(pady=(0, 10))

    def refresh() -> None:
        info = is_running(port, home)
        if info["http"]:
            status.set(f"运行中  {url}")
        elif info["running"]:
            status.set("正在拉起服务…")
        else:
            status.set("未在运行")
        root.after(2000, refresh)

    def open_page() -> None:
        webbrowser.open(url)

    def quit_desk() -> None:
        if not messagebox.askokcancel(
            "退出交付台",
            "关闭后 Pair 交付台会停止。正在跑的任务已落盘，下次打开可继续。\n\n确定退出？",
        ):
            return
        stop_desk(port, home)
        root.destroy()

    def hide_window() -> None:
        # The title-bar X used to stop the whole desk (desk.stop). Minimize
        # instead so an accidental close does not kill unattended runs.
        try:
            root.iconify()
        except tk.TclError:
            root.withdraw()

    btns = tk.Frame(root)
    btns.pack(pady=8)
    tk.Button(btns, text="打开页面", command=open_page, width=14, height=2).grid(row=0, column=0, padx=6)
    tk.Button(btns, text="退出交付台", command=quit_desk, width=14, height=2).grid(row=0, column=1, padx=6)
    tk.Label(
        root, text="点「退出交付台」才会停。关窗口只是最小化；关浏览器也不会停。",
        font=("Segoe UI", 8), fg="#555",
    ).pack(pady=(8, 0))

    root.protocol("WM_DELETE_WINDOW", hide_window)
    refresh()
    # Ensure the server is up even if the operator only launched the window.
    if not is_running(port, home)["http"]:
        start_desk(port, home, open_browser=False, open_app=False)
    root.mainloop()
    return 0
