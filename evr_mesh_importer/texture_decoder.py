import os
import struct
import subprocess
import tempfile
import json
from .textures import get_all_name_variations

def _find_file(base_dirs, hash_name):
    for d in base_dirs:
        if not d or not os.path.exists(d): continue
        for var in get_all_name_variations(hash_name):
            p = os.path.join(d, var)
            if os.path.isfile(p): return p
    return None

def decode_texture(low_hash: str, pcvr_extracted_dir: str, out_png_path: str, texconv_path: str) -> bool:
    """Decodes a low_hash game texture into a viewable PNG using the high quality payloads."""
    
    # 1. Locate low quality texture
    low_dirs = [os.path.join(pcvr_extracted_dir, "cgtextureresourceWin10")]
    for var in get_all_name_variations("4a4c32c49300b8a0"):
        low_dirs.append(os.path.join(pcvr_extracted_dir, var))
        
    low_path = _find_file(low_dirs, low_hash)
    
    if not low_path:
        print(f"[TextureDecoder] Could not find low quality texture: {low_hash}")
        return False
        
    with open(low_path, 'rb') as f:
        low_data = f.read()
        
    if len(low_data) < 256 + 128:
        print(f"[TextureDecoder] Low texture too small: {low_hash}")
        return False
        
    # 2. Extract high quality hashes from header (offset 0x40)
    high_hashes = []
    for i in range(0x40, 0x100, 8):
        chunk = low_data[i:i+8]
        if chunk == b'\xff' * 8:
            break
        h = struct.unpack('<Q', chunk)[0]
        high_hashes.append(f"{h:016x}")
        
    # 3. Read original DDS header
    dds_header = bytearray(low_data[256:256+148])
    if dds_header[:4] != b'DDS ':
        # Not all have DX10 headers, try standard 128 byte
        dds_header = bytearray(low_data[256:256+128])
        if dds_header[:4] != b'DDS ':
            print(f"[TextureDecoder] Invalid DDS header in {low_hash}")
            return False
            
    header_len = len(dds_header)
            
    orig_height = struct.unpack_from('<I', dds_header, 12)[0]
    orig_width = struct.unpack_from('<I', dds_header, 16)[0]
    orig_mips = struct.unpack_from('<I', dds_header, 28)[0]
    
    # 4. Read high quality payloads (largest to smallest)
    high_payloads = []
    high_dirs = [os.path.join(pcvr_extracted_dir, "RawTexturePackfileWin10")]
    for var in get_all_name_variations("ae49fad43254367a"):
        high_dirs.append(os.path.join(pcvr_extracted_dir, var))
    
    # Hashes are stored from smallest (e.g. 256x256) to largest (e.g. 2048x2048).
    # We want to prepend the largest first to build a valid DDS chain.
    for h in reversed(high_hashes):
        hp = _find_file(high_dirs, h)
        if hp:
            with open(hp, 'rb') as hf:
                high_payloads.append(hf.read())
        else:
            print(f"[TextureDecoder] Warning: missing high chunk {h}")
            
    # 5. Calculate new dimensions based on how many valid high quality chunks we found
    num_extra_mips = len(high_payloads)
    new_width = orig_width * (2 ** num_extra_mips)
    new_height = orig_height * (2 ** num_extra_mips)
    new_mips = orig_mips + num_extra_mips
    
    # 6. Update DDS header
    struct.pack_into('<I', dds_header, 12, new_height)
    struct.pack_into('<I', dds_header, 16, new_width)
    struct.pack_into('<I', dds_header, 28, new_mips)
    
    # 7. Write to temp DDS file
    fd, temp_dds = tempfile.mkstemp(suffix='.dds')
    with os.fdopen(fd, 'wb') as f:
        f.write(dds_header)
        for hp in high_payloads:
            f.write(hp)
        f.write(low_data[256 + header_len:]) # Append the rest of the low quality mipmaps
        
    # 8. Decode to PNG using Go texconv
    try:
        subprocess.run([texconv_path, 'decode', temp_dds, out_png_path], capture_output=True, check=True, cwd=os.path.dirname(texconv_path), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.CalledProcessError as e:
        print(f"[TextureDecoder] texconv failed: {e.stderr}")
        os.remove(temp_dds)
        return False
        
    os.remove(temp_dds)
    return os.path.exists(out_png_path)
