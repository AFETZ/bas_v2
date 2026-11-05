import argparse
import os
import re
import sys
import threading
import time

import customtkinter as ctk
from pymavlink import mavutil

DEFAULT_MAVLINK_ENDPOINT = "udp:127.0.0.1:14600"
ENDPOINT_ENV_VAR = "AMS_MAVLINK_ENDPOINT"
FORBIDDEN_DIRECT_ENDPOINTS = {
    ("udp", "127.0.0.1", 14550),
    ("tcp", "127.0.0.1", 5760),
    ("tcp", "127.0.0.1", 5770),
    ("tcp", "127.0.0.1", 5780),
    ("tcp", "127.0.0.1", 5790),
    ("tcp", "127.0.0.1", 5800),
}

master = None

# Define font and element size for larger readability
font_large = ("Arial", 16)
padding = 20
status_update_interval = 1
heartbeat_timeout = 3


def parse_mavlink_endpoint(endpoint):
    match = re.match(r"^(udp|udpin|udpout|tcp|tcpin|tcpout):([^:]+):([0-9]+)$", endpoint)
    if not match:
        return None
    scheme = match.group(1).lower()
    protocol = "tcp" if scheme.startswith("tcp") else "udp"
    host = match.group(2).lower()
    if host == "localhost":
        host = "127.0.0.1"
    return protocol, host, int(match.group(3))


def validate_mavlink_endpoint(endpoint):
    parsed = parse_mavlink_endpoint(endpoint)
    if parsed in FORBIDDEN_DIRECT_ENDPOINTS:
        blocked = f"{parsed[0]}:{parsed[1]}:{parsed[2]}"
        raise ValueError(
            f"Refusing direct MAVLink bypass endpoint {blocked}. "
            f"Use {ENDPOINT_ENV_VAR} or --endpoint with the network bridge endpoint."
        )
    return endpoint


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Control a drone via MAVLink position commands.")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get(ENDPOINT_ENV_VAR, DEFAULT_MAVLINK_ENDPOINT),
        help=(
            "pymavlink connection string. Defaults to the network bridge "
            f"endpoint, or ${ENDPOINT_ENV_VAR} when set."
        ),
    )
    parser.add_argument(
        "--heartbeat-timeout",
        type=float,
        default=float(os.environ.get("AMS_MAVLINK_HEARTBEAT_TIMEOUT", heartbeat_timeout)),
    )
    return parser.parse_args(argv)


def connect_mavlink(endpoint):
    safe_endpoint = validate_mavlink_endpoint(endpoint)
    print(f"Connecting to MAVLink endpoint: {safe_endpoint}")
    return mavutil.mavlink_connection(safe_endpoint)

def set_mode():
    mode = mode_combo_box.get()
    master.set_mode(mode)

def arm_drone():
    master.arducopter_arm()

def takeoff():
    altitude = float(takeoff_entry.get())
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0,
        0, 0, 0, 0, 0, 0, altitude
    )
    print(f"Taking off to altitude: {altitude}")

def move():
    px = float(move_x_entry.get())
    py = float(move_y_entry.get())
    pz = float(move_z_entry.get())
    master.mav.set_position_target_local_ned_send(
        0, master.target_system, master.target_component,
        mavutil.mavlink.MAV_FRAME_BODY_OFFSET_NED,
        int(0b100111111000),  # Type mask to ignore yaw and yaw rate, focus on position only
        px, py, pz,           # Position in x, y, z 
        0, 0, 0,              # Velocity in m/s
        0, 0, 0,              # Acceleration
        0, 0,                 # Yaw, Yaw rate
    )
    print(f"Moving x: {px}, y: {py}, z: {pz}")

def land():
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_NAV_LAND, 0,
        0, 0, 0, 0, 0, 0, 0
    )
    print("Landing initiated")

def update_status():
    while True:
        if master.wait_heartbeat(timeout=heartbeat_timeout):
            connection_status_label.configure(text="Connected", text_color="green")
        else:
            connection_status_label.configure(text="Disconnected", text_color="red")

        armed = master.motors_armed()
        ready_to_fly_label.configure(text="Ready to Fly" if armed else "Not Ready", text_color="green" if armed else "red")
        
        time.sleep(status_update_interval)

def build_gui():
    global arm_button
    global connection_status_label
    global land_button
    global mode_button
    global mode_combo_box
    global move_button
    global move_x_entry
    global move_y_entry
    global move_z_entry
    global ready_to_fly_label
    global root
    global takeoff_entry
    global takeoff_button

    ctk.set_appearance_mode("dark")  # Light or Dark mode
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.title("Drone Control GUI")

    # Status Labels
    connection_status_label = ctk.CTkLabel(root, text="Connecting...", text_color="orange", font=font_large)
    connection_status_label.grid(row=0, column=0, padx=padding, pady=padding, columnspan=2)

    ready_to_fly_label = ctk.CTkLabel(root, text="Not Ready", text_color="red", font=font_large)
    ready_to_fly_label.grid(row=0, column=2, padx=padding, pady=padding)

    # Mode control
    modes = list(mavutil.mode_mapping_acm.values())
    mode_combo_box = ctk.CTkComboBox(root, values=modes, font=font_large)
    mode_combo_box.set("GUIDED")
    mode_combo_box.grid(row=1, column=0, padx=padding, pady=padding)
    mode_button = ctk.CTkButton(root, text="Set Mode", command=set_mode, font=font_large)
    mode_button.grid(row=1, column=1, padx=padding, pady=padding)

    # Arm control
    arm_button = ctk.CTkButton(root, text="Arm Drone", command=arm_drone, font=font_large)
    arm_button.grid(row=1, column=2, padx=padding, pady=padding)

    # Takeoff control
    takeoff_label = ctk.CTkLabel(root, text="Takeoff Altitude:", font=font_large)
    takeoff_label.grid(row=2, column=0, padx=padding, pady=padding)
    takeoff_entry = ctk.CTkEntry(root, font=font_large)
    takeoff_entry.grid(row=2, column=1, padx=padding, pady=padding)
    takeoff_button = ctk.CTkButton(root, text="Takeoff", command=takeoff, font=font_large)
    takeoff_button.grid(row=2, column=2, padx=padding, pady=padding)

    # Move control
    move_x_label = ctk.CTkLabel(root, text="Move X:", font=font_large)
    move_x_label.grid(row=3, column=0, padx=padding, pady=padding)
    move_x_entry = ctk.CTkEntry(root, font=font_large)
    move_x_entry.grid(row=3, column=1, padx=padding, pady=padding)

    move_y_label = ctk.CTkLabel(root, text="Move Y:", font=font_large)
    move_y_label.grid(row=4, column=0, padx=padding, pady=padding)
    move_y_entry = ctk.CTkEntry(root, font=font_large)
    move_y_entry.grid(row=4, column=1, padx=padding, pady=padding)

    move_z_label = ctk.CTkLabel(root, text="Move Z:", font=font_large)
    move_z_label.grid(row=5, column=0, padx=padding, pady=padding)
    move_z_entry = ctk.CTkEntry(root, font=font_large)
    move_z_entry.grid(row=5, column=1, padx=padding, pady=padding)

    move_button = ctk.CTkButton(root, text="Move", command=move, font=font_large)
    move_button.grid(row=6, column=1, padx=padding, pady=padding)
    land_button = ctk.CTkButton(root, text="Land", command=land, font=font_large)
    land_button.grid(row=6, column=2, padx=padding, pady=padding)

    return root


def main(argv=None):
    global heartbeat_timeout
    global master

    args = parse_args(argv)
    heartbeat_timeout = args.heartbeat_timeout
    try:
        master = connect_mavlink(args.endpoint)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    app = build_gui()

    # Start status update thread
    status_thread = threading.Thread(target=update_status, daemon=True)
    status_thread.start()

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
