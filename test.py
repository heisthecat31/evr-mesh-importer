"""
CGML Diagnostic Tool - Compare original vs exported CGML files
Usage: python diagnose_cgml.py <original_gpu> <original_primary> <exported_gpu> <exported_primary>
"""

import struct
import sys

def parse_cgml_primary(data, label):
    """Parse a CGML Primary file and return structured info."""
    n_meta = len(data)
    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    arrays = []
    off = 0
    for stride in sizes:
        if off + 4 > n_meta:
            break
        count = struct.unpack_from("<I", data, off)[0]
        base = off + 4
        arrays.append({'base': base, 'count': count, 'stride': stride})
        off = base + count * stride
    
    tail_offset = off
    
    print(f"\n{'='*60}")
    print(f"CGML Primary Analysis: {label}")
    print(f"{'='*60}")
    print(f"File size: {len(data)} bytes")
    
    for i, arr in enumerate(arrays):
        print(f"\nArray {i}: base=0x{arr['base']:X}, count={arr['count']}, stride=0x{arr['stride']:X}")
        
        if arr['count'] > 0 and arr['count'] <= 50:
            for j in range(min(arr['count'], 5)):
                rec_off = arr['base'] + j * arr['stride']
                
                if i == 2:  # Array 2 - mesh metadata
                    base_offset = struct.unpack_from("<I", data, rec_off + 0x128)[0]
                    stream0_size = struct.unpack_from("<I", data, rec_off + 0x130)[0]
                    vertex_count = struct.unpack_from("<I", data, rec_off + 0x13c)[0]
                    vc2 = struct.unpack_from("<I", data, rec_off + 0x140)[0]
                    vc3 = struct.unpack_from("<I", data, rec_off + 0x14c)[0]
                    print(f"  [{j}] base=0x{base_offset:X}, s0_size={stream0_size}, vc={vertex_count}, vc2={vc2}, vc3={vc3}")
                
                elif i == 5:  # Array 5 - index buffer ranges
                    ib_offset = struct.unpack_from("<I", data, rec_off + 0x00)[0]
                    ib_size = struct.unpack_from("<I", data, rec_off + 0x04)[0]
                    rk = struct.unpack_from("<I", data, rec_off + 0x08)[0]
                    rx = struct.unpack_from("<I", data, rec_off + 0x0c)[0]
                    print(f"  [{j}] ib_off=0x{ib_offset:X}, ib_size={ib_size}, rk={rk}, rx={rx}")
                
                elif i == 1:  # Array 1 - stream records
                    vc = struct.unpack_from("<I", data, rec_off + 0x48)[0]
                    print(f"  [{j}] vc={vc}")
    
    if tail_offset + 16 <= n_meta:
        tail_a, mesh_data_end, gpu_size = struct.unpack_from("<IIQ", data, tail_offset)
        print(f"\nTail: a={tail_a}, mesh_end={mesh_data_end}, gpu_size={gpu_size}")
    
    return arrays, tail_offset


def dump_gpu_hex(data, offset, size, label):
    """Dump a hex view of GPU data at a specific offset."""
    print(f"\n{label} at 0x{offset:X} ({size} bytes):")
    chunk = data[offset:min(offset+size, len(data))]
    for i in range(0, min(len(chunk), 128), 16):
        hex_str = ' '.join(f'{b:02X}' for b in chunk[i:i+16])
        print(f"  0x{offset+i:08X}: {hex_str}")


def main():
    if len(sys.argv) != 5:
        print("Usage: python diagnose_cgml.py <orig_gpu> <orig_primary> <exported_gpu> <exported_primary>")
        sys.exit(1)
    
    # Read files
    with open(sys.argv[1], 'rb') as f:
        orig_gpu = f.read()
    with open(sys.argv[2], 'rb') as f:
        orig_primary = f.read()
    with open(sys.argv[3], 'rb') as f:
        exp_gpu = f.read()
    with open(sys.argv[4], 'rb') as f:
        exp_primary = f.read()
    
    # Parse both Primaries
    orig_arrays, orig_tail = parse_cgml_primary(orig_primary, "ORIGINAL")
    exp_arrays, exp_tail = parse_cgml_primary(exp_primary, "EXPORTED")
    
    # Compare Array 2
    print(f"\n{'='*60}")
    print(f"COMPARISON: Array 2 (mesh metadata)")
    print(f"{'='*60}")
    orig_arr2 = orig_arrays[2]
    exp_arr2 = exp_arrays[2]
    for j in range(min(orig_arr2['count'], exp_arr2['count'])):
        o_rec = orig_arr2['base'] + j * orig_arr2['stride']
        e_rec = exp_arr2['base'] + j * exp_arr2['stride']
        
        o_base = struct.unpack_from("<I", orig_primary, o_rec + 0x128)[0]
        o_s0 = struct.unpack_from("<I", orig_primary, o_rec + 0x130)[0]
        o_vc = struct.unpack_from("<I", orig_primary, o_rec + 0x13c)[0]
        
        e_base = struct.unpack_from("<I", exp_primary, e_rec + 0x128)[0]
        e_s0 = struct.unpack_from("<I", exp_primary, e_rec + 0x130)[0]
        e_vc = struct.unpack_from("<I", exp_primary, e_rec + 0x13c)[0]
        
        status_base = "✓" if o_base == e_base else "✗"
        status_s0 = "✓" if o_s0 == e_s0 else "✗"
        status_vc = "✓" if o_vc == e_vc else "✗"
        
        print(f"  [{j}] base: {o_base} → {e_base} {status_base}")
        print(f"       s0_size: {o_s0} → {e_s0} {status_s0}")
        print(f"       vc: {o_vc} → {e_vc} {status_vc}")
    
    # Compare Array 5
    print(f"\n{'='*60}")
    print(f"COMPARISON: Array 5 (index buffers)")
    print(f"{'='*60}")
    orig_arr5 = orig_arrays[5]
    exp_arr5 = exp_arrays[5]
    for j in range(min(orig_arr5['count'], exp_arr5['count'])):
        o_rec = orig_arr5['base'] + j * orig_arr5['stride']
        e_rec = exp_arr5['base'] + j * exp_arr5['stride']
        
        o_ib_off = struct.unpack_from("<I", orig_primary, o_rec + 0x00)[0]
        o_ib_sz = struct.unpack_from("<I", orig_primary, o_rec + 0x04)[0]
        
        e_ib_off = struct.unpack_from("<I", exp_primary, e_rec + 0x00)[0]
        e_ib_sz = struct.unpack_from("<I", exp_primary, e_rec + 0x04)[0]
        
        print(f"  [{j}] ib_off: 0x{o_ib_off:X} → 0x{e_ib_off:X}")
        print(f"       ib_size: {o_ib_sz} → {e_ib_sz}")
    
    # Dump first submesh GPU data from both
    if orig_arr2['count'] > 0:
        o_rec = orig_arr2['base']
        e_rec = exp_arr2['base']
        
        o_base = struct.unpack_from("<I", orig_primary, o_rec + 0x128)[0]
        o_s0 = struct.unpack_from("<I", orig_primary, o_rec + 0x130)[0]
        o_vc = struct.unpack_from("<I", orig_primary, o_rec + 0x13c)[0]
        
        e_base = struct.unpack_from("<I", exp_primary, e_rec + 0x128)[0]
        e_s0 = struct.unpack_from("<I", exp_primary, e_rec + 0x130)[0]
        e_vc = struct.unpack_from("<I", exp_primary, e_rec + 0x13c)[0]
        
        dump_gpu_hex(orig_gpu, o_base, o_s0, "ORIGINAL Stream-0")
        dump_gpu_hex(exp_gpu, e_base, e_s0, "EXPORTED Stream-0")
        
        # Dump first few vertices from Stream-1
        o_s1_start = o_base + o_s0
        e_s1_start = e_base + e_s0
        dump_gpu_hex(orig_gpu, o_s1_start, min(o_vc * 28, 256), "ORIGINAL Stream-1 (first verts)")
        dump_gpu_hex(exp_gpu, e_s1_start, min(e_vc * 28, 256), "EXPORTED Stream-1 (first verts)")

if __name__ == '__main__':
    main()