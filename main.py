import customtkinter as ctk
import os
import sys

def main():
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")
    
    app = ctk.CTk()
    app.geometry("800x600")
    app.title("PKG_D0Wn")
    
    # Skeleton elements
    label = ctk.CTkLabel(app, text="PKG_D0Wn Initialization")
    label.pack(pady=20)
    
    app.mainloop()

if __name__ == "__main__":
    main()
