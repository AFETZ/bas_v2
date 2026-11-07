FROM osrf/ros:humble-desktop@sha256:46efd5ebec72c5da478027890ac534cbcb0c2cf93482559e0f35a9fc3c05d47a

# Add a non-root user with a specified UID and GID matching the host system
ARG USERNAME=ubuntu
ARG USER_UID=1000
ARG USER_GID=$USER_UID

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ENV GZ_VERSION=harmonic

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
RUN sudo apt update

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
    cd ardupilot && \
    git checkout 3c073f9a09590307e99f49f960dd0f4dac7fc5bb && \
    git submodule update --init --recursive && \
    Tools/environment_install/install-prereqs-ubuntu.sh -y && \
    sudo apt-get install -y python3-pexpect

COPY ardupilot_ros2_exact.repos /workspace/ardupilot_ros2_exact.repos
COPY ardupilot_gz_exact.repos /workspace/ardupilot_gz_exact.repos
COPY 00-gazebo.list /workspace/00-gazebo.list
COPY requirements-radio.in /workspace/requirements-radio.in
COPY requirements-radio.lock /workspace/requirements-radio.lock
COPY mpl_toolkits/__init__.py /workspace/mpl_toolkits/__init__.py

# Create Ardupilot workspace
RUN mkdir -p /workspace/ardu_ws/src && \
    cd /workspace/ardu_ws && \
    vcs import --recursive --input /workspace/ardupilot_ros2_exact.repos src && \
    /bin/bash -c "source /opt/ros/humble/setup.bash" && \
    rosdep install --from-paths src --ignore-src -y


# Install MicroXRCEDDSGen build dependency
RUN sudo apt install -y default-jre && \
    cd /workspace/ardu_ws/ && \
    git clone --recurse-submodules https://github.com/ardupilot/Micro-XRCE-DDS-Gen.git && \
    cd Micro-XRCE-DDS-Gen && \
    git checkout 4bc7ec2c7f77ae89cd216432f05470ad99f7faa1 && \
    git submodule update --init --recursive && \
    printf '%s  %s\n' \
      c5a643cf80162e665cc228f7b16f343fef868e47d3a4836f62e18b7e17ac018a \
      gradle/wrapper/gradle-wrapper.jar | sha256sum -c - && \
    cp gradle/wrapper/gradle-wrapper.properties /tmp/gradle-wrapper.properties.original && \
    printf '\ndistributionSha256Sum=%s\n' \
      7ba68c54029790ab444b39d7e293d3236b2632631fb5f2e012bb28b4ff669e4b \
      >> gradle/wrapper/gradle-wrapper.properties && \
    ./gradlew assemble && \
    mv /tmp/gradle-wrapper.properties.original gradle/wrapper/gradle-wrapper.properties && \
    echo "export PATH=\$PATH:$PWD/scripts" >> ~/.bashrc


RUN sudo apt install -y python3-colcon-common-extensions \
    ros-humble-ament-cmake \
    ros-humble-ament-lint \
    ros-humble-ament-cmake-core \
    python3-rosdep

# Build Ardupilot workspace
RUN cd /workspace/ardu_ws && \
    source /opt/ros/humble/setup.bash && \
    rosdep update && \
    rosdep install -y --from-paths src --ignore-src --rosdistro $ROS_DISTRO && \
    export PATH=$PATH:$PWD/Micro-XRCE-DDS-Gen/scripts && \
    python3 -m pip install --user --no-cache-dir \
      "setuptools==79.0.1" "pexpect==4.8.0" "future==1.0.0" && \
    colcon build --packages-up-to ardupilot_dds_tests --event-handlers=console_cohesion+ --symlink-install

# Install Gazebo Harmonic
RUN sudo apt install curl lsb-release gnupg wget -y && \
    curl -fsSL https://packages.osrfoundation.org/gazebo.gpg -o /tmp/gazebo.gpg && \
    printf '%s  %s\n' \
      15d0300460e0c9c0efb21fa9770c0bf228988a07d71c4fac985c2161954a25a4 \
      /tmp/gazebo.gpg | sha256sum -c - && \
    sudo install -m 0644 /tmp/gazebo.gpg /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] https://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/gazebo-stable.list > /dev/null && \
    sudo apt update && \
    sudo apt install gz-harmonic -y

# Add Gazebo Harmonic Ardupilot packages
RUN cd /workspace/ardu_ws && \
    source /opt/ros/humble/setup.bash && \
    export PATH=$PATH:$PWD/Micro-XRCE-DDS-Gen/scripts && \
    vcs import --input /workspace/ardupilot_gz_exact.repos --recursive src && \
    echo "export GZ_VERSION=harmonic" >> ~/.bashrc && \
    sudo install -m 0644 /workspace/00-gazebo.list /etc/ros/rosdep/sources.list.d/00-gazebo.list && \
    rosdep update && \
    rosdep install --from-paths src --ignore-src -y && \
    colcon build --packages-up-to ardupilot_gz_bringup ros_gz_image --symlink-install

################################################################
## Network/radio packet-in-the-loop runtime dependencies      ##
################################################################

RUN sudo apt-get update && \
    sudo apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      ninja-build \
      pkg-config \
      python3-dev \
      iproute2 \
      iptables \
      nftables \
      tcpdump \
      util-linux \
      netcat-openbsd \
      iputils-ping && \
    sudo rm -rf /var/lib/apt/lists/*

RUN printf '%s  %s\n' \
      7c0bf57854a1c1abd4ddd14f1c9e7559b21b3e5f7bc4ffbae7be4a3124907e84 \
      /workspace/requirements-radio.in | sha256sum -c - && \
    printf '%s  %s\n' \
      15204b43c4077ed1371d411480630749a15165a7ee82c92377201af6783e619a \
      /workspace/requirements-radio.lock | sha256sum -c - && \
    python3 -m pip install --user --no-cache-dir --force-reinstall \
      --require-hashes -r /workspace/requirements-radio.lock && \
    test -f /usr/lib/python3/dist-packages/matplotlib-3.5.1-nspkg.pth && \
    sudo rm /usr/lib/python3/dist-packages/matplotlib-3.5.1-nspkg.pth && \
    install -m 0644 /workspace/mpl_toolkits/__init__.py \
      "$HOME/.local/lib/python3.10/site-packages/mpl_toolkits/__init__.py" && \
    python3 -m pip check && \
    source /opt/ros/humble/setup.bash && \
    python3 -c 'import cv2, cv_bridge, mitsuba, mpl_toolkits.mplot3d, numpy, sionna.rt; assert numpy.__version__ == "1.26.4"'

# Copy post create script
COPY setup.sh /workspace/setup.sh
RUN sudo chmod +x /workspace/setup.sh && \
    sudo git config --system --add safe.directory /workspace/multiagent_simulation && \
    sudo git config --system --add safe.directory /workspace/multiagent_simulation/.git
