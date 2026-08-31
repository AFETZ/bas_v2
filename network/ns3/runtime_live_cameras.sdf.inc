    <model name="native_radio_command_post">
      <static>true</static>
      <pose>5 0 2 0 0 0</pose>
      <link name="marker">
        <visual name="command_post_marker">
          <geometry><box><size>4 4 3</size></box></geometry>
          <material><ambient>0.1 0.3 1 1</ambient><diffuse>0.1 0.3 1 1</diffuse></material>
        </visual>
      </link>
    </model>
    <model name="native_radio_overview_camera">
      <static>true</static>
      <!-- Forward (+X) view across CP and the five active flight positions. -->
      <pose>5 -35 35 0 0.55 0</pose>
      <link name="camera_link">
        <sensor name="overview" type="camera">
          <always_on>true</always_on><update_rate>5</update_rate><visualize>false</visualize>
          <topic>/native_radio/overview/image</topic>
          <camera><horizontal_fov>1.65</horizontal_fov><image><width>640</width><height>360</height><format>R8G8B8</format></image><clip><near>0.1</near><far>350</far></clip></camera>
        </sensor>
      </link>
    </model>
    <model name="native_radio_obstacle_camera">
      <static>true</static>
      <!-- Oblique view of UAV1's southern-to-northern obstacle crossing. -->
      <pose>35 45 40 0 0.48 0.95</pose>
      <link name="camera_link">
        <sensor name="obstacle" type="camera">
          <always_on>true</always_on><update_rate>5</update_rate><visualize>false</visualize>
          <topic>/native_radio/obstacle/image</topic>
          <camera><horizontal_fov>1.35</horizontal_fov><image><width>640</width><height>360</height><format>R8G8B8</format></image><clip><near>0.1</near><far>250</far></clip></camera>
        </sensor>
      </link>
    </model>
    <model name="native_radio_uav_focus_camera">
      <static>true</static>
      <pose>35 -25 32 0 0.55 0.35</pose>
      <link name="camera_link">
        <sensor name="uav_focus" type="camera">
          <always_on>true</always_on><update_rate>5</update_rate><visualize>false</visualize>
          <topic>/native_radio/uav_focus/image</topic>
          <camera><horizontal_fov>1.20</horizontal_fov><image><width>640</width><height>360</height><format>R8G8B8</format></image><clip><near>0.1</near><far>200</far></clip></camera>
        </sensor>
      </link>
    </model>
