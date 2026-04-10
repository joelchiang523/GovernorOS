import os
import hashlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from collections import defaultdict


def find_duplicates(path):
    """
    Find duplicate files by SHA256 hash in the given path (recursive).
    Returns a dictionary mapping hash to list of file paths.
    Only returns groups with more than one file (actual duplicates).
    """
    hash_to_files = defaultdict(list)
    
    for root, dirs, files in os.walk(path):
        for filename in files:
            filepath = os.path.join(root, filename)
            try:
                file_hash = calculate_file_hash(filepath)
                hash_to_files[file_hash].append(filepath)
            except (IOError, OSError):
                continue
    
    # Filter to only groups with duplicates (more than 1 file)
    return {h: files for h, files in hash_to_files.items() if len(files) > 1}


def calculate_file_hash(filepath):
    """Calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


class DuplicateFinderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("重複檔案搜尋工具")
        self.root.geometry("900x650")
        
        self.selected_path = None
        self.duplicates = {}
        self.file_checkboxes = {}
        self.group_colors = [
            "#e8f5e9", "#fff3e0", "#e3f2fd", "#fce4ec", 
            "#f3e5f5", "#e0f2f1", "#fff8e1", "#ffebee",
            "#e1f5fe", "#f1f8e9", "#fffde7", "#fce4ec"
        ]
        
        self.create_widgets()
    
    def create_widgets(self):
        # Top frame with buttons
        top_frame = ttk.Frame(self.root, padding="10")
        top_frame.pack(fill=tk.X)
        
        self.path_label = ttk.Label(top_frame, text="掃描目錄：未選擇", width=50)
        self.path_label.pack(side=tk.LEFT, padx=5)
        
        self.select_btn = ttk.Button(top_frame, text="選擇掃描目錄", command=self.select_directory)
        self.select_btn.pack(side=tk.LEFT, padx=5)
        
        self.scan_btn = ttk.Button(top_frame, text="開始掃描", command=self.scan_duplicates)
        self.scan_btn.pack(side=tk.LEFT, padx=5)
        
        self.delete_btn = ttk.Button(top_frame, text="刪除選定檔案", command=self.delete_selected)
        self.delete_btn.pack(side=tk.LEFT, padx=5)
        
        # Treeview with scrollbar
        tree_frame = ttk.Frame(self.root, padding="10")
        tree_frame.pack(fill=tk.BOTH, expand=True)
        
        # Create scrollbar
        scrollbar = ttk.Scrollbar(tree_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Create Treeview
        self.tree = ttk.Treeview(tree_frame, columns=("path", "size"), show="headings", yscrollcommand=scrollbar.set)
        self.tree.pack(fill=tk.BOTH, expand=True)
        
        # Configure columns
        self.tree.heading("path", text="檔案路徑")
        self.tree.heading("size", text="大小 (KB)")
        self.tree.column("path", width=700)
        self.tree.column("size", width=100)
        
        scrollbar.config(command=self.tree.yview)
        
        # Status bar
        self.status_var = tk.StringVar(value="準備就緒")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
    
    def select_directory(self):
        directory = filedialog.askdirectory()
        if directory:
            self.selected_path = directory
            self.path_label.config(text=f"掃描目錄：{directory}")
            self.status_var.set(f"已選擇目錄：{directory}")
    
    def scan_duplicates(self):
        if not self.selected_path:
            messagebox.showwarning("警告", "請先選擇掃描目錄")
            return
        
        self.status_var.set("正在掃描...")
        self.root.update()
        
        # Clear existing tree and checkboxes
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.file_checkboxes.clear()
        
        # Find duplicates
        self.duplicates = find_duplicates(self.selected_path)
        
        # Display results
        self.display_duplicates()
        
        self.status_var.set(f"掃描完成，發現 {len(self.duplicates)} 組重複檔案")
    
    def display_duplicates(self):
        if not self.duplicates:
            self.status_var.set("未發現重複檔案")
            return
        
        for idx, (file_hash, files) in enumerate(self.duplicates.items()):
            # Add header for this group
            header_text = f"重複群組 {idx + 1} (SHA256: {file_hash[:16]}...)"
            self.tree.insert("", "end", text=header_text, values=("", ""), tags=("group_header",))
            self.tree.tag_configure("group_header", font=("TkDefaultFont", 10, "bold"))
            
            # Add files in this group
            color = self.group_colors[idx % len(self.group_colors)]
            for filepath in files:
                try:
                    size_kb = os.path.getsize(filepath) / 1024
                    checkbox_var = tk.BooleanVar()
                    self.file_checkboxes[filepath] = checkbox_var
                    
                    self.tree.insert("", "end", text=filepath, values=(filepath, f"{size_kb:.2f}"), tags=("file",))
                    self.tree.tag_configure("file", background=color)
                except OSError:
                    continue
    
    def delete_selected(self):
        selected_files = [filepath for filepath, var in self.file_checkboxes.items() if var.get()]
        
        if not selected_files:
            messagebox.showinfo("提示", "請勾選要刪除的檔案")
            return
        
        # Show confirmation dialog
        confirm_msg = f"確定要刪除以下 {len(selected_files)} 個檔案嗎？\n\n"
        for filepath in selected_files[:5]:
            confirm_msg += f"- {filepath}\n"
        if len(selected_files) > 5:
            confirm_msg += f"... 以及另外 {len(selected_files) - 5} 個檔案"
        
        if messagebox.askyesno("確認刪除", confirm_msg):
            deleted_count = 0
            for filepath in selected_files:
                try:
                    os.remove(filepath)
                    deleted_count += 1
                except OSError as e:
                    messagebox.showerror("錯誤", f"無法刪除檔案：{filepath}\n錯誤：{e}")
            
            messagebox.showinfo("完成", f"已成功刪除 {deleted_count} 個檔案")
            self.scan_duplicates()  # Refresh the list


if __name__ == "__main__":
    root = tk.Tk()
    app = DuplicateFinderApp(root)
    root.mainloop()
