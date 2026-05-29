"""PyEmbedBuilder starter entry point."""


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        print("This is the default entry point.")
        print("Replace main.py or set a custom entry point for your app.")
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        return 0

    root = tk.Tk()
    root.title("PyEmbedBuilder Starter")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")

    text = (
        "This is the default entry point.\n"
        "Replace main.py or set a custom entry point for your app."
    )
    lbl = ttk.Label(frame, text=text, justify="center", anchor="center")
    lbl.grid(row=0, column=0, pady=(0, 12))

    btn = ttk.Button(frame, text="Close", command=root.destroy)
    btn.grid(row=1, column=0)
    btn.focus_set()

    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = root.winfo_reqheight()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    root.geometry(f"{w}x{h}+{x}+{y}")

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
