#!/usr/bin/env python3
import os
import sys
import uuid
import shutil

BASE_DIR = "/srv/agy-manager"
SHARED_DIR = os.path.join(BASE_DIR, "shared")
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")

def create_profile(profile_name: str, copy_token_from: str = None):
    profile_dir = os.path.join(PROFILES_DIR, profile_name)
    home_dir = os.path.join(profile_dir, "home")
    gemini_dir = os.path.join(home_dir, ".gemini")
    cli_dir = os.path.join(gemini_dir, "antigravity-cli")

    os.makedirs(cli_dir, exist_ok=True)
    os.makedirs(os.path.join(cli_dir, "log"), exist_ok=True)
    os.makedirs(os.path.join(cli_dir, "crashes"), exist_ok=True)

    # Set 0700 permissions
    os.chmod(profile_dir, 0o700)
    os.chmod(home_dir, 0o700)
    os.chmod(gemini_dir, 0o700)
    os.chmod(cli_dir, 0o700)

    # Symlink .gemini/config -> shared/config
    config_symlink = os.path.join(gemini_dir, "config")
    if not os.path.lexists(config_symlink):
        os.symlink(os.path.join(SHARED_DIR, "config"), config_symlink)

    # Shared symlinks in antigravity-cli
    shared_items = [
        "conversations",
        "annotations",
        "presence",
        "brain",
        "cache",
        "bin",
        "builtin",
        "settings.json",
        "conversation_summaries.db",
    ]

    for item in shared_items:
        dst = os.path.join(cli_dir, item)
        src = os.path.join(SHARED_DIR, item)
        if not os.path.lexists(dst):
            os.symlink(src, dst)

    # Copy jetski_state if exists
    jetski_src = os.path.join(SHARED_DIR, "jetski_state.pbtxt")
    jetski_dst = os.path.join(cli_dir, "jetski_state.pbtxt")
    if os.path.exists(jetski_src) and not os.path.exists(jetski_dst):
        shutil.copy2(jetski_src, jetski_dst)
        os.chmod(jetski_dst, 0o600)

    # Generate installation_id
    inst_dst = os.path.join(cli_dir, "installation_id")
    if not os.path.exists(inst_dst):
        with open(inst_dst, "w") as f:
            f.write(str(uuid.uuid4()))
        os.chmod(inst_dst, 0o644)

    # Copy token if requested
    token_dst = os.path.join(cli_dir, "antigravity-oauth-token")
    if copy_token_from and os.path.exists(copy_token_from):
        shutil.copy2(copy_token_from, token_dst)
        os.chmod(token_dst, 0o600)
        print(f"[{profile_name}] Token copied from {copy_token_from}")

    print(f"[{profile_name}] Initialized successfully at {profile_dir}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: profile_init.py <profile_name> [copy_token_from]")
        sys.exit(1)
    name = sys.argv[1]
    token_from = sys.argv[2] if len(sys.argv) > 2 else None
    create_profile(name, token_from)
