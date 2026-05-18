def print_header(file_path, label):
    try:
        with open(file_path, "rb") as f:
            header = f.read(48)
            hex_string = " ".join(f"{b:02X}" for b in header)
            print(f"=== {label} ===")
            print(f"Path: {file_path}")
            print(f"Hex:  {hex_string}\n")
    except Exception as e:
        print(f"Failed to read {label}: {e}")

# Replace these with an actual working file name and a broken summer file name
working_file = r"J:\EchoVR-Tools-Launcher\evr-mesh-importer-main\models\GPU\CGInstancedModelResource\8d7b40499db09c83"
broken_file = r"J:\EchoVR-Tools-Launcher\evr-mesh-importer-main\modelssummer\GPU\CGInstancedModelResource\de7e9e5689ea5265"

print_header(working_file, "WORKING MODEL (YESTERDAY)")
print_header(broken_file, "BROKEN SUMMER MODEL (TODAY)")