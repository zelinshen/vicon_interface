from dataclasses import dataclass
from threading import Thread
from typing import Any
import time

import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.duration import Duration
from nav_msgs.msg import Odometry
from geometry_msgs.msg import PoseWithCovarianceStamped
from geometry_msgs.msg import TransformStamped
from std_msgs.msg import Float32
from tf2_ros import TransformBroadcaster

from pyvicon_datastream import tools
import pyvicon_datastream as pv
import tf_transformations

import numpy as np

from vicon_interface.vicon_data_buffer import ViconDataBuffer, ViconDataFilter


@dataclass
class TrackedObject:
    name: str
    child_frame_id: str
    buffer: ViconDataBuffer
    pose_publisher: Any
    odom_publisher: Any
    filtered_odom_publisher: Any
    latency_publisher: Any


class ViconStreamNode(Node):
    def __init__(self):
        super().__init__("vicon_stream_node")
        self.viacon_tracker_ip_ = (
            self.declare_parameter("vicon_tracker_ip", rclpy.Parameter.Type.STRING)
            .get_parameter_value()
            .string_value
        )
        self.viacon_object_names_ = list(
            self.declare_parameter("vicon_object_names", rclpy.Parameter.Type.STRING_ARRAY)
            .get_parameter_value()
            .string_array_value
        )
        if not self.viacon_object_names_:
            self.get_logger().fatal("At least one Vicon object name must be provided via `vicon_object_names`.")
            raise ValueError("Missing Vicon object names parameter")
        self.min_states_ = np.array(
            self.declare_parameter("min_states", rclpy.Parameter.Type.DOUBLE_ARRAY)
            .get_parameter_value()
            .double_array_value
        )
        self.max_states_ = np.array(
            self.declare_parameter("max_states", rclpy.Parameter.Type.DOUBLE_ARRAY)
            .get_parameter_value()
            .double_array_value
        )
        self.parent_frame_id_ = (
            self.declare_parameter("parent_frame_id", "map")
            .get_parameter_value()
            .string_value
        )
        self.child_frame_ids_ = list(
            self.declare_parameter("child_frame_ids", rclpy.Parameter.Type.STRING_ARRAY)
            .get_parameter_value()
            .string_array_value
        )
        if self.child_frame_ids_ and len(self.child_frame_ids_) != len(self.viacon_object_names_):
            self.get_logger().fatal(f"`child_frame_ids` must match the length of `vicon_object_names` (expected {len(self.viacon_object_names_)}, got {len(self.child_frame_ids_)}).")
            raise ValueError("Mismatched child frame list length")
        self.poll_vicon_dt_ = (
            self.declare_parameter("poll_vicon_dt", 0.01)
            .get_parameter_value()
            .double_value
        )
        self.filtered_odom_dt_ = (
            self.declare_parameter("filtered_odom_dt", 0.02)
            .get_parameter_value()
            .double_value
        )
        self.rolling_window_size_ = (
            self.declare_parameter("rolling_window_size", 10)
            .get_parameter_value()
            .integer_value
        )
        self.publish_tf_ = (
            self.declare_parameter("publish_tf", True).get_parameter_value().bool_value
        )

        self.get_logger().info(
            f"Connecting to Viacon tracker at {self.viacon_tracker_ip_}..."
        )
        self.tracker_ = tools.ObjectTracker(self.viacon_tracker_ip_)
        if self.tracker_.is_connected:
            self.get_logger().info(
                f"Connection to {self.viacon_tracker_ip_} successful"
            )
        else:
            self.get_logger().fatal(f"Connection to {self.viacon_tracker_ip_} failed")
            raise Exception(f"Connection to {self.viacon_tracker_ip_} failed")

        self.tracked_objects_: dict[str, TrackedObject] = {}
        for idx, object_name in enumerate(self.viacon_object_names_):
            child_frame = (self.child_frame_ids_[idx] if idx < len(self.child_frame_ids_) else f"{object_name}/base_link")
            pose_topic = f"vicon_pose/{object_name}"
            odom_topic = f"odometry/{object_name}"
            filtered_odom_topic = f"odometry/filtered/{object_name}"
            latency_topic = f"vicon_latency/{object_name}"
            tracked_object = TrackedObject(
                name=object_name,
                child_frame_id=child_frame,
                buffer=ViconDataBuffer(6, self.rolling_window_size_),
                pose_publisher=self.create_publisher(PoseWithCovarianceStamped, pose_topic, 1),
                odom_publisher=self.create_publisher(Odometry, odom_topic, 1),
                filtered_odom_publisher=self.create_publisher(Odometry, filtered_odom_topic, 1),
                latency_publisher=self.create_publisher(Float32, latency_topic, 1),
            )
            self.tracked_objects_[object_name] = tracked_object

        self.last_frame_nums_ = {name: 0 for name in self.viacon_object_names_}

        if self.publish_tf_:
            self.tf_broadcaster_ = TransformBroadcaster(self)

        self.cb_group_poll_viacon_ = MutuallyExclusiveCallbackGroup()
        self.cb_group_filtered_odom_ = MutuallyExclusiveCallbackGroup()

        # self.timer_ = self.create_timer(self.poll_vicon_dt_, self.timer_callback, self.cb_group_poll_viacon_)
        self.poll_vicon_thread = Thread(target=self.timer_callback)
        self.poll_vicon_thread.start()

        self.filtered_odom_timer_ = self.create_timer(
            self.filtered_odom_dt_,
            self.filtered_odom_timer_callback,
            self.cb_group_filtered_odom_,
        )

    def timer_callback(self):
        while True:
            begin = time.monotonic()
            if not self.tracker_.is_connected:
                continue
            frame_result = self.tracker_.vicon_client.get_frame()
            end_get_frame_time = time.monotonic()
            # print(f"Time to get frame: {end_get_frame_time - begin:.6f}s")

            transforms_to_publish = []
            for object_name, tracked_object in self.tracked_objects_.items():
                if frame_result == pv.Result.Success:
                    latency = self.tracker_.vicon_client.get_latency_total()
                    framenumber = self.tracker_.vicon_client.get_frame_number()
                    position = self.tracker_._get_object_position(object_name)
                    if latency is None or framenumber is None or not position:
                        self.get_logger().warn(f"Missing object `{object_name}` data in frame.",throttle_duration_sec=1.0)
                        continue
                else:
                    self.get_logger().warn(f"Cannot get the frame with `{object_name}`.", throttle_duration_sec=1.0)
                    continue

                grab_time = self.get_clock().now()
                latency = float(latency)
                frame_time = grab_time - Duration(seconds=latency)
                frame_stamp = frame_time.to_msg()
                frame_num = framenumber
                if frame_num == self.last_frame_nums_[object_name]:
                    continue
                self.last_frame_nums_[object_name] = frame_num
                # print(f"For object `{object_name}`, got frame {frame_num} at time {frame_stamp} with latency {latency:.4f}s.")
                try:
                    obj = position[0]
                    _, _, x, y, z, roll, pitch, yaw = obj
                    q = tf_transformations.quaternion_from_euler(roll, pitch, yaw, "rxyz")
                    roll, pitch, yaw = tf_transformations.euler_from_quaternion(q, "sxyz")
                    tracked_object.buffer.add_pose((x, y, z, roll, pitch, yaw), latency)
                except Exception as e:
                    self.get_logger().warn(
                        f"Vicon dropped a frame for `{object_name}`: {e}",
                        throttle_duration_sec=1.0,
                    )
                    # continue

                v_body, ang_v_body, timestamp = tracked_object.buffer.get_latest_velocity()
                if timestamp == 0:
                    continue
                pose, _ = tracked_object.buffer.get_latest_pose()
                x, y, z, roll, pitch, yaw = pose

                states = np.array(
                    [
                        x,
                        y,
                        z,
                        roll,
                        pitch,
                        yaw,
                        v_body[0],
                        v_body[1],
                        v_body[2],
                        ang_v_body[0],
                        ang_v_body[1],
                        ang_v_body[2],
                    ]
                )
                states = np.clip(states, self.min_states_, self.max_states_)
                (
                    x,
                    y,
                    z,
                    roll,
                    pitch,
                    yaw,
                    v_body[0],
                    v_body[1],
                    v_body[2],
                    ang_v_body[0],
                    ang_v_body[1],
                    ang_v_body[2],
                ) = states

                odom_msg = Odometry()
                odom_msg.header.stamp = frame_stamp
                odom_msg.header.frame_id = self.parent_frame_id_
                odom_msg.child_frame_id = tracked_object.child_frame_id
                odom_msg.pose.pose.position.x = x
                odom_msg.pose.pose.position.y = y
                odom_msg.pose.pose.position.z = z
                q = tf_transformations.quaternion_from_euler(roll, pitch, yaw)
                odom_msg.pose.pose.orientation.x = q[0]
                odom_msg.pose.pose.orientation.y = q[1]
                odom_msg.pose.pose.orientation.z = q[2]
                odom_msg.pose.pose.orientation.w = q[3]
                odom_msg.twist.twist.linear.x = v_body[0]
                odom_msg.twist.twist.linear.y = v_body[1]
                odom_msg.twist.twist.linear.z = v_body[2]
                odom_msg.twist.twist.angular.x = ang_v_body[0]
                odom_msg.twist.twist.angular.y = ang_v_body[1]
                odom_msg.twist.twist.angular.z = ang_v_body[2]
                tracked_object.odom_publisher.publish(odom_msg)

                pose_msg = PoseWithCovarianceStamped()
                pose_msg.pose.pose.position.x = x
                pose_msg.pose.pose.position.y = y
                pose_msg.pose.pose.position.z = z
                pose_msg.pose.pose.orientation.x = q[0]
                pose_msg.pose.pose.orientation.y = q[1]
                pose_msg.pose.pose.orientation.z = q[2]
                pose_msg.pose.pose.orientation.w = q[3]
                pose_msg.header.stamp = odom_msg.header.stamp
                pose_msg.header.frame_id = self.parent_frame_id_
                tracked_object.pose_publisher.publish(pose_msg)

                if self.publish_tf_:
                    transform = TransformStamped()
                    transform.header.stamp = odom_msg.header.stamp
                    transform.header.frame_id = self.parent_frame_id_
                    transform.child_frame_id = tracked_object.child_frame_id
                    transform.transform.translation.x = x
                    transform.transform.translation.y = y
                    transform.transform.translation.z = z
                    transform.transform.rotation.x = q[0]
                    transform.transform.rotation.y = q[1]
                    transform.transform.rotation.z = q[2]
                    transform.transform.rotation.w = q[3]
                    transforms_to_publish.append(transform)

                latency_msg = Float32()
                latency_msg.data = latency
                tracked_object.latency_publisher.publish(latency_msg)

            if self.publish_tf_ and transforms_to_publish:
                # Publish the full set of TF in a single message
                self.tf_broadcaster_.sendTransform(transforms_to_publish)

            end = time.monotonic()
            if end - begin < self.poll_vicon_dt_:
                time.sleep(self.poll_vicon_dt_ - (end - begin))

    def filtered_odom_timer_callback(self):
        for tracked_object in self.tracked_objects_.values():
            if not tracked_object.buffer.is_ready():
                continue
            position, orientation, _ = tracked_object.buffer.get_interpolated_pose()
            v_body, ang_v_body, _ = tracked_object.buffer.get_latest_velocity()

            x, y, z = position
            roll, pitch, yaw = orientation
            states = np.array(
                [
                    x,
                    y,
                    z,
                    roll,
                    pitch,
                    yaw,
                    v_body[0],
                    v_body[1],
                    v_body[2],
                    ang_v_body[0],
                    ang_v_body[1],
                    ang_v_body[2],
                ]
            )
            states = np.clip(states, self.min_states_, self.max_states_)
            (
                x,
                y,
                z,
                roll,
                pitch,
                yaw,
                v_body[0],
                v_body[1],
                v_body[2],
                ang_v_body[0],
                ang_v_body[1],
                ang_v_body[2],
            ) = states

            odom_msg = Odometry()
            odom_msg.header.stamp = self.get_clock().now().to_msg()
            odom_msg.header.frame_id = self.parent_frame_id_
            odom_msg.child_frame_id = tracked_object.child_frame_id
            odom_msg.pose.pose.position.x = x
            odom_msg.pose.pose.position.y = y
            odom_msg.pose.pose.position.z = z
            q = tf_transformations.quaternion_from_euler(roll, pitch, yaw)
            odom_msg.pose.pose.orientation.x = q[0]
            odom_msg.pose.pose.orientation.y = q[1]
            odom_msg.pose.pose.orientation.z = q[2]
            odom_msg.pose.pose.orientation.w = q[3]
            odom_msg.twist.twist.linear.x = v_body[0]
            odom_msg.twist.twist.linear.y = v_body[1]
            odom_msg.twist.twist.linear.z = v_body[2]
            odom_msg.twist.twist.angular.x = ang_v_body[0]
            odom_msg.twist.twist.angular.y = ang_v_body[1]
            odom_msg.twist.twist.angular.z = ang_v_body[2]
            tracked_object.filtered_odom_publisher.publish(odom_msg)


def main(args=None):
    rclpy.init(args=args)
    vicon_stream_node = ViconStreamNode()
    executor = MultiThreadedExecutor()
    executor.add_node(vicon_stream_node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    # vicon_stream_node.destroy_node()
    # rclpy.shutdown()


if __name__ == "__main__":
    main()
