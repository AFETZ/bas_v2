FROM osrf/ros:humble-desktop

# Add a non-root user with a specified UID and GID matching the host system
ARG USERNAME=ubuntu
ARG USER_UID=1000
ARG USER_GID=$USER_UID

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

# Ensure user and group exist with the specified UID/GID
RUN groupadd --gid $USER_GID $USERNAME && \
    useradd -s /bin/bash --uid $USER_UID --gid $USER_GID -m $USERNAME

# Add sudo support for the non-root user
RUN apt-get update && \
    apt-get install -y sudo git && \
    echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/$USERNAME && \
    chmod 0440 /etc/sudoers.d/$USERNAME

# Preemptively fix permissions for the user during build
RUN mkdir -p /workspace && \
    chown -R $USER_UID:$USER_GID /workspace

# Set the working directory and user
WORKDIR /workspace
USER $USERNAME

# Update all packages
RUN sudo apt update && sudo apt upgrade -y

# Install Git
RUN sudo apt install -y git

# Rosdep update
RUN rosdep update

# Source the ROS setup file
RUN echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc

################################################################
## ARDUPILOT SITL with ROS 2 Humble and Gazebo Harmonic setup ##
################################################################

RUN sudo apt install ros-dev-tools python3-pip -y

ENV USER=${USERNAME}
ENV LOGNAME=${USERNAME}
ENV HOME=/home/${USERNAME}

# Setup Ardupilot
RUN git clone https://github.com/ArduPilot/ardupilot.git && \
    sudo pip3 install future && \
    cd ardupilot && \
    git submodule update --init --recursive && \
    Tools/environment_install/install-prereqs-ubuntu.sh -y || true && \
    sudo apt-get install -y python3-pexpect

# Create Ardupilot workspace
RUN mkdir -p /workspace/ardu_ws/src && \
    cd /workspace/ardu_ws && \
    vcs import --recursive --input  https://raw.githubusercontent.com/ArduPilot/ardupilot/master/Tools/ros2/ros2.repos src && \
    /bin/bash -c "source /opt/ros/humble/setup.bash" && \
    rosdep install --from-paths src --ignore-src -r -y


# Install MicroXRCEDDSGen build dependency
RUN sudo apt install -y default-jre && \
    cd /workspace/ardu_ws/ && \
    git clone --recurse-submodules https://github.com/ardupilot/Micro-XRCE-DDS-Gen.git && \
    cd Micro-XRCE-DDS-Gen && \
    ./gradlew assemble && \
    echo "export PATH=\$PATH:$PWD/scripts" >> ~/.bashrc


RUN sudo apt install -y python3-colcon-common-extensions \
    ros-humble-ament-cmake \
    ros-humble-ament-lint \
    ros-humble-ament-cmake-core \
    python3-rosdep

# Build Ardupilot workspace
RUN cd /workspace/ardu_ws && \
    source /opt/ros/humble/setup.bash && \
    (rosdep update || true) && \
    rosdep install -y --from-paths src --ignore-src --rosdistro $ROS_DISTRO && \
    export PATH=$PATH:$PWD/Micro-XRCE-DDS-Gen/scripts && \
    python3 -m pip install --user --no-cache-dir "setuptools<80" pexpect future && \
    colcon build --packages-up-to ardupilot_dds_tests --event-handlers=console_cohesion+ --symlink-install

# Install Gazebo Harmonic
RUN sudo apt install curl lsb-release gnupg wget -y && \
    sudo curl https://packages.osrfoundation.org/gazebo.gpg --output /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/gazebo-stable.list > /dev/null && \
    sudo apt update && \
    sudo apt install gz-harmonic -y

# Add Gazebo Harmonic Ardupilot packages
RUN cd /workspace/ardu_ws && \
    source /opt/ros/humble/setup.bash && \
    export PATH=$PATH:$PWD/Micro-XRCE-DDS-Gen/scripts && \
    vcs import --input https://raw.githubusercontent.com/ArduPilot/ardupilot_gz/main/ros2_gz.repos --recursive src && \
    echo "export GZ_VERSION=harmonic" >> ~/.bashrc && \
    sudo wget https://raw.githubusercontent.com/osrf/osrf-rosdep/master/gz/00-gazebo.list -O /etc/ros/rosdep/sources.list.d/00-gazebo.list && \
    (rosdep update || true) && \
    export GZ_VERSION=harmonic && rosdep install --from-paths src --ignore-src -r -y && \
    colcon build --packages-up-to ardupilot_gz_bringup ros_gz_image --symlink-install

RUN python3 -m pip install --user --no-cache-dir customtkinter

# Copy post create script
COPY setup.sh /workspace/setup.sh
RUN sudo chmod +x /workspace/setup.sh
