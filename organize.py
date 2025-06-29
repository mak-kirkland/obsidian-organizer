import os
import re
import shutil
import yaml
import json
import sys
import argparse
import logging

def parse_args():
    parser = argparse.ArgumentParser(description="Organize Obsidian vault based on tags")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    return parser.parse_args()

args = parse_args()

logging.basicConfig(
    level=logging.DEBUG if args.verbose else logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Load configuration from YAML file
with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# Assign configuration to variables
VAULT_ROOT = config['vault_root']
DEFAULT_FOLDER = config['default_folder']
CATEGORY_RULES = config['category_rules']
SUBCATEGORY_RULES = config['subcategory_rules']
TAG_CONSOLIDATION = config['tag_consolidation']

YAML_FRONTMATTER_REGEX = re.compile(r"(?s)^---\n(.*?)\n---\n")

def flatten_subcategory_order(subcategory_rules):
    ordered_tags = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, val in node.items():
                ordered_tags.append(key.lower())
                walk(val)
        elif isinstance(node, str):
            ordered_tags.append(node.lower())

    for cat_key, branches in subcategory_rules.items():
        walk(branches)

    return ordered_tags

SUBCATEGORY_ORDER = flatten_subcategory_order(SUBCATEGORY_RULES)

def build_subcategory_paths(subcategory_rules, category_rules):
    flat_map = {}

    def walk(parent_path, node):
        if isinstance(node, list):
            for item in node:
                walk(parent_path, item)
        elif isinstance(node, dict):
            for key, val in node.items():
                folder_name = key.capitalize()
                full_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
                flat_map[key.lower()] = full_path
                walk(full_path, val)
        elif isinstance(node, str):
            folder_name = node.capitalize()
            full_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
            flat_map[node.lower()] = full_path

    for cat_key, branches in subcategory_rules.items():
        # Get main folder from category_rules, fallback to cat_key itself if not found
        main_folder = category_rules.get(cat_key, cat_key)
        walk(main_folder, branches)

    return flat_map

# Reverse lookup: folder name (like "6_Lore") -> tag (like "lore")
FOLDER_TO_CATEGORY = {v.lower(): k.lower() for k, v in CATEGORY_RULES.items()}
# Build flat map once
SUBCATEGORY_PATHS = build_subcategory_paths(SUBCATEGORY_RULES, CATEGORY_RULES)

# === FUNCTIONS ===

def normalize_tags(tags):
    return [t.lower() for t in tags if isinstance(t, str)]

def parse_yaml_frontmatter(filepath):
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logging.warn(f"⚠️ Failed to read {filepath}: {e}")
        return {}

    match = YAML_FRONTMATTER_REGEX.match(content)
    if not match:
        return {}

    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception as e:
        logging.warn(f"⚠️ YAML parse error in {filepath}: {e}")
        return {}

def write_yaml_frontmatter(filepath, data, original_content):
    new_yaml = yaml.safe_dump(data, sort_keys=False).strip()

    if YAML_FRONTMATTER_REGEX.search(original_content):
        # Replace existing frontmatter
        def replacer(match):
            return f"---\n{new_yaml}\n---\n"
        new_content = YAML_FRONTMATTER_REGEX.sub(replacer, original_content, count=1)
    else:
        # No frontmatter exists, prepend new frontmatter block
        new_content = f"---\n{new_yaml}\n---\n\n{original_content}"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)

def consolidate_tags(tags):
    """Apply all tag consolidation rules and remove replaced tags"""
    consolidated = []
    replaced_tags = set()

    for input_tag in tags:
        replacement = TAG_CONSOLIDATION.get(input_tag, input_tag)
        if replacement not in consolidated:
            consolidated.append(replacement)
        if replacement != input_tag:
            replaced_tags.add(input_tag)

    # Remove replaced tags from consolidated list if they are still there
    final_tags = [tag for tag in consolidated if tag not in replaced_tags]
    return final_tags

def add_parent_tags_for_subcategories(tags):
    tags_set = set(tags)
    added_tags = False

    for tag in tags:
        path = SUBCATEGORY_PATHS.get(tag)
        if not path:
            continue

        parts = path.split("/")
        top_level_folder = parts[0].lower()
        top_level_tag = FOLDER_TO_CATEGORY.get(top_level_folder)

        # Add top level tag if missing, append at the end
        if top_level_tag and top_level_tag not in tags_set:
            tags.append(top_level_tag)
            tags_set.add(top_level_tag)
            added_tags = True

        # Add intermediate parts if missing, append at the end
        for part in parts[1:-1]:
            normalized_part = part.lower()
            if normalized_part not in tags_set:
                tags.append(normalized_part)
                tags_set.add(normalized_part)
                added_tags = True

    return tags, added_tags

def classify_file(yaml_data):
    tags = yaml_data.get("tags") or []

    tags = normalize_tags(tags)
    tags = consolidate_tags(tags)

    tags, _ = add_parent_tags_for_subcategories(tags)

    matching_main_keys = [key for key in CATEGORY_RULES if key.lower() in tags]

    if not matching_main_keys:
        main_folder = DEFAULT_FOLDER
    else:
        main_folder = CATEGORY_RULES[matching_main_keys[0]]

    candidate_subfolders = []

    for tag in tags:
        sub_path = SUBCATEGORY_PATHS.get(tag)
        if sub_path and sub_path.lower().startswith(main_folder.lower()):
            depth = sub_path.count("/")
            order_index = SUBCATEGORY_ORDER.index(tag) if tag in SUBCATEGORY_ORDER else 1_000_000
            candidate_subfolders.append((depth, order_index, sub_path))

    if candidate_subfolders:
        # Sort by descending depth, then ascending order_index
        candidate_subfolders.sort(key=lambda x: (-x[0], x[1]))
        subfolder = candidate_subfolders[0][2]
    else:
        subfolder = None

    return main_folder, subfolder, tags

def move_file(filepath, dest_folder, vault_root):
    dest_path_folder = os.path.join(vault_root, dest_folder)
    os.makedirs(dest_path_folder, exist_ok=True)

    filename = os.path.basename(filepath)
    dest_path = os.path.join(dest_path_folder, filename)

    if os.path.exists(dest_path):
        raise FileExistsError(f"❌ File already exists at destination: {dest_path}")

    logging.debug(f"📁 Moving '{filename}' to '{dest_folder}/'")
    shutil.move(filepath, dest_path)
    return dest_path

def update_tags_in_file(filepath, new_tags):
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logging.warn(f"⚠️ Failed to read {filepath} for updating tags: {e}")
        return False

    match = YAML_FRONTMATTER_REGEX.match(content)
    if match:
        yaml_data = yaml.safe_load(match.group(1)) or {}
    else:
        yaml_data = {}

    yaml_data['tags'] = new_tags
    write_yaml_frontmatter(filepath, yaml_data, content)
    logging.debug(f"📝 Updated tags in '{filepath}'")
    return True

def update_indexes(tag_to_files_map, vault_root):
    index_dir = os.path.join(vault_root, "_indexes")
    os.makedirs(index_dir, exist_ok=True)

    # Get all current index files
    current_index_files = {
        os.path.splitext(f)[0].lower(): os.path.join(index_dir, f)
        for f in os.listdir(index_dir)
        if f.endswith(".md")
    }

    # Determine the tags we now care about (consolidated ones)
    updated_tags = set(tag_to_files_map.keys())

    # Remove obsolete index files
    for tag, path in current_index_files.items():
        if tag not in updated_tags:
            os.remove(path)
            logging.debug(f"🗑️ Removed obsolete index: {tag}.md")

    # Rebuild valid index files with proper tagging
    for tag, files in tag_to_files_map.items():
        # Create content with tag reference
        lines = [f"# Index for #{tag}"]

        for filepath in sorted(files):
            note_name = os.path.splitext(os.path.basename(filepath))[0]
            lines.append(f"- [[{note_name}]]")

        content = "\n".join(lines)

        index_path = os.path.join(index_dir, f"_{tag}.md")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(content)
        logging.debug(f"📄 Updated index for: {tag}")

def organize_vault(vault_root):
    logging.info(f"🔎 Scanning vault: {vault_root}")

    tag_to_files_map = {}

    for root, _, files in os.walk(vault_root):
        rel_root = os.path.relpath(root, vault_root)
        if rel_root == ".":
            rel_root = ""

        # Skip _indexes folder anywhere in path
        if "_indexes" in rel_root.split(os.sep):
            continue

        for filename in files:
            if not filename.endswith(".md"):
                continue

            filepath = os.path.join(root, filename)
            yaml_data = parse_yaml_frontmatter(filepath)
            main_folder, subfolder, updated_tags = classify_file(yaml_data)
            orig_tags = yaml_data.get("tags") or []
            orig_tags_lower = normalize_tags(orig_tags)
            updated_tags_lower = normalize_tags(updated_tags)

            if set(updated_tags_lower) != set(orig_tags_lower):
                update_tags_in_file(filepath, updated_tags)

            for tag in updated_tags_lower:
                tag_to_files_map.setdefault(tag, []).append(filepath)

            # Determine target folder relative to vault root
            target_folder = subfolder if subfolder else main_folder
            target_folder_norm = os.path.normpath(target_folder)

            # Current file folder relative to vault root
            file_current_folder = os.path.relpath(root, vault_root)
            file_current_folder_norm = os.path.normpath(file_current_folder)

            # Move if current folder is different from target folder
            if file_current_folder_norm.lower() != target_folder_norm.lower():
                try:
                    move_file(filepath, target_folder, vault_root)
                except FileExistsError as e:
                    logging.info(f"⚠️ Skipped moving due to existing file: {e}")

    update_indexes(tag_to_files_map, vault_root)
    logging.info("✅ Vault organization complete!")

if __name__ == "__main__":
    organize_vault(VAULT_ROOT)
