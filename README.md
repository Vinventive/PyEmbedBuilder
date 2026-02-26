# PyEmbedBuilder

![Version](https://img.shields.io/badge/version-1.0.0_alpha-orange)
![Platform](https://img.shields.io/badge/platform-Windows_10%2F11-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**PyEmbedBuilder**  is a user-friendly GUI for creating portable, open-source Python applications that you can share with other Windows users who don’t feel confident setting up a full Python environment themselves. It uses a .bat file to launch your (.py) Python app in a clear and secure way. It lunches as easily as any regular (.exe) executable software, but without the need to compile binary executables or pay hundreds of dollars to have your apps signed. It fully exposes the launcher and application source code to all end users, making it better suited for open for security audits open-source distribution rather than fully closed-source software. I’m simply sharing it with anyone interested, as I originally built this tool for my own use.

<img width="1280" height="720" alt="image" src="https://github.com/user-attachments/assets/3371eb8a-36a2-4b3b-baa9-c8476d489b3f" />


It automates the complex process of downloading embedded Python distributions, verifying their integrity, bootstrapping `pip`, installing dependencies, and securely extracting official components (like Tcl/Tk for Tkinter support) into a clean, portable folder structure.
# [Direct Link to Download](https://github.com/Vinventive/PyEmbedBuilder/releases/download/v1.0.0-alpha/PyEmbedBuilder_v1.0.0-alpha.7z)
<img src="https://count.getloli.com/@PyEmbedBuilder?name=PyEmbedBuilder&theme=booru-lewd&padding=7&offset=0&align=top&scale=1.5&pixelated=1&darkmode=auto" />

---

## 🔒 Security & Integrity

Security is the core design principle of PyEmbedBuilder:

*   **Strict Verification:** All downloads from `python.org` are verified.
*   **HTTPS Only:** Enforced TLS for network operations.
*   **Audit Trail:** Generates a comprehensive `security_audit.log` for every build, recording source URLs.
*   **Path Sanitization:** Automatically strips absolute paths from `pip` metadata (`.dist-info`) to prevent and minimize local system information leakage when distributing portable apps with embedded environments.
*   **Zip-Slip Protection:** Validates all archive extractions against directory traversal attacks.

## ✨ Key Features

*   **Wizard-Style Interface:** Modern, accessible Tkinter GUI with Dark/Light Mode, High Contrast Accessibility Mode and text scaling support.
*   **Full Python Support:** 
    *   Download any stable Python version (≥ 3.12.10).
    *   **Optional Component Extraction:** Automatically add `Scripts`, `tcl`, `Lib`, `libs`, and `include` folders—enabling full standard library support (including `tkinter`) in a portable embedded environment.
*   **Dependency Management:** Import your `requirements.txt` to pre-install packages into the portable environment.
*   **Portable Output:** 
    *   Generates `.bat` launchers automatically.
    *   Configures `._pth` files correctly for full isolation.
    *   Creates fully independent and portable Python environments in a `..\My Projects\` folder by default.
*   **Smart Caching:** Optional downloads cache to save bandwidth when creating multiple similar projects (by default builder auto-clears cache upon each successful build).

## 🚀 Getting Started

### 1. Download & Verify
Download the release archive and its signature:
*   `PyEmbedBuilder_(version).7z`
*   `PyEmbedBuilder_(version).7z.sha256`

**Verify the integrity (PowerShell):**
```powershell
Get-FileHash .\PyEmbedBuilder_(version).7z -Algorithm SHA256
# Compare output with the content of PyEmbedBuilder_(version).7z.sha256
```

### 2. Installation
PyEmbedBuilder is fully portable.  No installation is required. It has been successfully built with a running instance of itself.
The standalone portable version has been compressed with free 7-Zip software. To avoid any issues with .7z archives, I advise installing the free official 7-Zip software for Windows, available [here](https://7-zip.org/download.html).
1.  Extract `PyEmbedBuilder.7z` to a location of your choice (e.g., `C:\PyEmbedBuilder`).
2.  Navigate to the extracted folder.

### 3. Usage
Double-click **`launch_pyembed_builder.bat`** to start the application.

1.  **Create Project:** 
    *   Name your project.
    *   Select a Python version (Recommended or Custom).
    *   (Optional) Select a `requirements.txt` file.
    *   (Recommended) Check **"Add full stdlib..."** to include Tkinter, pip, and standard headers.
2.  **Review:** Check the build plan and security parameters.
3.  **Build:** Watch the automated process:
    *   Download & Verification
    *   Core Extraction
    *   Optional Components Extraction
    *   pip Bootstrap 
    *   Optional Packages Installation (requirements.txt)
    *   Paths Sanitization
4.  **Complete:** By default your portable environment is ready in `..\My Projects\<Project-Name>`.

## 📦 Output Structure

The builder creates self-contained environments structured for distribution:

```text
My Projects/
└── Project-Name/
    ├── launch.bat                 # Your App Launcher
    ├── (your_app_here).py         # Entry Point to Your Python Application
    ├── install_dependencies.bat   # Add new libraries/packages
    ├── list_dependencies.bat      # List all currently installed libraries/packages
    ├── uninstall_dependencies.bat # Remove any libraries/packages
    ├── requirements.txt           # List of recomended required libraries in the current Python project (provide your own when building)
    ├── build_manifest.json        # App manifest for debuging (remove it before public distribution)
    ├── python-embed/              # The portable Python environment
    │   ├── python.exe             # Python CLI Shell Binary
    │   ├── pythonw.exe            # Python Window Mode Shell Binary
    │   ├── Lib/                   # Standard library & site-packages
    │   ├── Scripts/               # Pip and other CLI tools
    │   └── ...
    └── ...
```

*Disclaimer: This is a very early working alpha version of the software. Always test and verify your portable Python apps before sharing them with others.*
