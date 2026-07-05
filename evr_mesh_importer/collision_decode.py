import struct
import os

def extract_collision_heuristic(filepath):
    """
    Heuristically scans a proprietary Echo VR collision binary for valid 3D float vertices.
    Returns a flat list of all vertices found.
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Collision file not found: {filepath}")
        
    with open(filepath, "rb") as f:
        data = f.read()

    size = len(data)
    
    # Extract all floats
    all_floats = []
    # Using a 4-byte step to stay aligned
    for i in range(0, size - 4, 4):
        f = struct.unpack_from("<f", data, i)[0]
        all_floats.append(f)

    # Heuristic: Find blocks of consecutive valid floats (vertices)
    # A vertex block is defined as > 12 consecutive floats between -1000 and 1000
    vertices = []
    current_block = []

    for f in all_floats:
        # Check if the float is within reasonable spatial bounds for the game
        # and not a tiny denormalized number, unless it is strictly 0.0
        if -1000.0 < f < 1000.0 and (abs(f) > 0.0001 or f == 0.0):
            current_block.append(f)
        else:
            # If we hit an invalid float, evaluate the current block
            if len(current_block) >= 12: # At least 4 vertices
                # Ensure it's a multiple of 3
                trim = len(current_block) % 3
                if trim != 0:
                    current_block = current_block[:-trim]
                
                for j in range(0, len(current_block), 3):
                    vertices.append((current_block[j], current_block[j+1], current_block[j+2]))
            
            # Reset the block
            current_block = []

    # Final check if the file ended cleanly on a block
    if len(current_block) >= 12:
        trim = len(current_block) % 3
        if trim != 0:
            current_block = current_block[:-trim]
        for j in range(0, len(current_block), 3):
            vertices.append((current_block[j], current_block[j+1], current_block[j+2]))

    return vertices
