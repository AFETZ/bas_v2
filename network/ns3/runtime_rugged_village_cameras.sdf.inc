    <model name="native_radio_rugged_overview_camera">
      <static>true</static>
      <pose>-40 40 1350 0 1.570796 0</pose>
      <link name="link">
        <sensor name="sensor" type="camera">
          <always_on>true</always_on>
          <update_rate>2</update_rate>
          <topic>/native_radio/overview/image</topic>
          <camera><horizontal_fov>1.25</horizontal_fov><image><width>1280</width><height>720</height><format>R8G8B8</format></image><clip><near>1</near><far>12000</far></clip></camera>
        </sensor>
      </link>
    </model>
    <model name="native_radio_rugged_obstacle_camera">
      <static>true</static>
      <pose>-80 -850 520 0 0.58 1.570796</pose>
      <link name="link">
        <sensor name="sensor" type="camera">
          <always_on>true</always_on>
          <update_rate>2</update_rate>
          <topic>/native_radio/obstacle/image</topic>
          <camera><horizontal_fov>1.15</horizontal_fov><image><width>1280</width><height>720</height><format>R8G8B8</format></image><clip><near>1</near><far>5000</far></clip></camera>
        </sensor>
      </link>
    </model>
    <model name="native_radio_rugged_uav_focus_camera">
      <static>true</static>
      <pose>-500 0 260 0 0.42 0</pose>
      <link name="link">
        <sensor name="sensor" type="camera">
          <always_on>true</always_on>
          <update_rate>4</update_rate>
          <topic>/native_radio/uav_focus/image</topic>
          <camera><horizontal_fov>1.25</horizontal_fov><image><width>1280</width><height>720</height><format>R8G8B8</format></image><clip><near>0.5</near><far>4000</far></clip></camera>
        </sensor>
      </link>
    </model>
