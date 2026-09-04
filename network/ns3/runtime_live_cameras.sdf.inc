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
      <!-- Wide top-down view covering the CP, five launch points, and UAV1 route. -->
      <pose>50 55 140 0 1.5707 0</pose>
      <link name="camera_link">
        <sensor name="overview" type="camera">
          <always_on>true</always_on><update_rate>2</update_rate><visualize>false</visualize>
          <topic>/native_radio/overview/image</topic>
          <camera><horizontal_fov>1.4</horizontal_fov><image><width>1280</width><height>720</height><format>R8G8B8</format></image><clip><near>0.1</near><far>350</far></clip></camera>
        </sensor>
      </link>
    </model>
    <model name="native_radio_obstacle_camera">
      <static>true</static>
      <!-- Close top-down view of UAV1 at the verified obstructed waypoint. -->
      <pose>80 110 70 0 1.5707 0</pose>
      <link name="camera_link">
        <sensor name="obstacle" type="camera">
          <always_on>true</always_on><update_rate>2</update_rate><visualize>false</visualize>
          <topic>/native_radio/obstacle/image</topic>
          <camera><horizontal_fov>1.0</horizontal_fov><image><width>960</width><height>540</height><format>R8G8B8</format></image><clip><near>0.1</near><far>250</far></clip></camera>
        </sensor>
      </link>
    </model>
    <model name="native_radio_uav_focus_camera">
      <static>true</static>
      <!-- Detailed top-down view of the five-UAV holding line. -->
      <pose>50 0 90 0 1.5707 1.5708</pose>
      <link name="camera_link">
        <sensor name="uav_focus" type="camera">
          <always_on>true</always_on><update_rate>2</update_rate><visualize>false</visualize>
          <topic>/native_radio/uav_focus/image</topic>
          <camera><horizontal_fov>1.2</horizontal_fov><image><width>960</width><height>540</height><format>R8G8B8</format></image><clip><near>0.1</near><far>200</far></clip></camera>
        </sensor>
      </link>
    </model>
