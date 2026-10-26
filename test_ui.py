import customtkinter as ctk

app = ctk.CTk()
app.geometry("800x600")

scroll = ctk.CTkScrollableFrame(app)
scroll.pack(fill="both", expand=True)

def add_items():
    print("Adding items...")
    for i in range(10452):
        lbl = ctk.CTkLabel(scroll, text=f"Item {i}")
        lbl.pack()
    print("Done adding items")

app.after(1000, add_items)
app.mainloop()
