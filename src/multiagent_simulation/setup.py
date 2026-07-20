import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'multiagent_simulation'

def get_model_files():
    model_files = []
    for root, dirs, files in os.walk('models'):
        for file in files:
            # Get the relative path for each file
            file_path = os.path.join(root, file)
            install_dir = os.path.join('share', package_name, root)  # Preserve directory structure
            model_files.append((install_dir, [file_path]))
    return model_files

def get_world_files():
    worlds_files = []
    for root, dirs, files in os.walk('worlds'):
        for file in files:
            # Get the relative path for each file
            file_path = os.path.join(root, file)
            install_dir = os.path.join('share', package_name, root)  # Preserve directory structure
            worlds_files.append((install_dir, [file_path]))
    return worlds_files

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'), glob('config/*')),
        (os.path.join('share', package_name, 'rviz'), glob('rviz/*')),
    ] + get_model_files() + get_world_files(),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Gilbert Tanner',
    maintainer_email='gilbert.tanner@aau.at',
    description='Ardupilot multiagent simulation environment',
    license='GPL-3.0',
    entry_points={
        'console_scripts': [
            'move_drone = multiagent_simulation.move_drone:main',
            'fly_trajectory = multiagent_simulation.fly_trajectory',
            'initialization_flight = multiagent_simulation.initialization_flight',
        ],
    },
)
