import time
from threading import Lock
import numpy as np
import tf_transformations


class ViconDataBuffer:
    def __init__(self, state_dim, rolling_window_size, offset_xyz=None, offset_R=None) -> None:
        """Rolling pose buffer with finite-difference velocity.

        offset_xyz / offset_R describe the pose of the frame to report relative to the tracked rigid
        body's own frame (T_marker->child): the child origin expressed in marker coordinates,
        and the child orientation as a rotation matrix. They are applied per sample BEFORE the
        finite difference, so the reported velocity is the child frame's velocity.
        """
        self.past_poses_ = np.zeros((0, state_dim))
        self.past_times_ = np.zeros((0, 1))
        self.rolling_window_size_ = rolling_window_size
        self.lock_ = Lock()

        self.offset_xyz_ = (
            np.zeros(3) if offset_xyz is None else np.asarray(offset_xyz, dtype=float)
        )
        self.offset_R_ = np.eye(3) if offset_R is None else np.asarray(offset_R, dtype=float)
        self.has_offset_ = bool(
            np.any(self.offset_xyz_) or not np.allclose(self.offset_R_, np.eye(3))
        )

        self.v_ = None
        self.a_ = None

    def add_pose(self, pose, latency):
        with self.lock_:
            time_stamp = time.monotonic() - latency
            x, y, z, roll, pitch, yaw = pose
            x /= 1000.0
            y /= 1000.0
            z /= 1000.0

            # marker -> child frame, applied here so the finite difference below sees the
            # child's trajectory.
            # R is used to express the velocity in the child's own frame; in CHILD orientation, not the marker's.
            R = tf_transformations.euler_matrix(roll, pitch, yaw, "sxyz")[:3, :3]
            if self.has_offset_:
                x, y, z = np.array([x, y, z]) + R @ self.offset_xyz_
                R = R @ self.offset_R_
                m = np.eye(4)
                m[:3, :3] = R
                roll, pitch, yaw = tf_transformations.euler_from_matrix(m, "sxyz")
            if len(self.past_poses_) == self.rolling_window_size_:
                self.past_poses_ = self.past_poses_[1:]
                self.past_times_ = self.past_times_[1:]
            self.past_poses_ = np.vstack(
                (self.past_poses_, np.array([x, y, z, roll, pitch, yaw]))
            )
            self.past_times_ = np.append(self.past_times_, time_stamp)

            if len(self.past_poses_) == self.rolling_window_size_:
                self.v_ = (self.past_poses_[-1, :3] - self.past_poses_[0, :3]) / (
                    self.past_times_[-1] - self.past_times_[0]
                )
                self.a_ = (self.past_poses_[-1, 3:] - self.past_poses_[0, 3:]) / (
                    self.past_times_[-1] - self.past_times_[0]
                )
                self.a_ = np.arctan2(np.sin(self.a_), np.cos(self.a_))
            else:
                self.v_ = np.zeros(3)
                self.a_ = np.zeros(3)

            self.v_ = np.dot(R.T, self.v_)
            self.a_ = np.dot(R.T, self.a_)

    def get_latest_pose(self):
        with self.lock_:
            if len(self.past_poses_) == 0:
                return np.zeros(6), 0
            return self.past_poses_[-1], self.past_times_[-1]

    def get_latest_velocity(self):
        with self.lock_:
            if self.v_ is None:
                return np.zeros(3), np.zeros(3), 0
            return self.v_, self.a_, self.past_times_[-1]

    def get_interpolated_pose(self):
        with self.lock_:
            time_stamp = time.monotonic()
            if len(self.past_poses_) < 2:
                return np.zeros(3), np.zeros(3), time_stamp

            time_diff = time_stamp - self.past_times_[-1]

            position = self.past_poses_[-1, :3]
            roll, pitch, yaw = self.past_poses_[-1, 3:]
            R = tf_transformations.euler_matrix(roll, pitch, yaw, "sxyz")[:3, :3]
            v_world = np.dot(R, self.v_)
            a_world = np.dot(R, self.a_)
            position += time_diff * v_world
            orientation = self.past_poses_[-1, 3:] + time_diff * a_world
            return position, orientation, time_stamp

    def is_ready(self):
        with self.lock_:
            return len(self.past_poses_) == self.rolling_window_size_


class ViconDataFilter:
    def __init__(self, cutoff_freq, dt) -> None:
        self.last_pose_ = None
        self.last_time_ = None
        self.cutoff_freq_ = cutoff_freq
        self.epow_ = 1 - np.exp(-2 * np.pi * cutoff_freq * dt)
        self.lock_ = Lock()

        self.v_ = None
        self.a_ = None

    def add_pose(self, pose, latency):
        with self.lock_:
            time_stamp = time.monotonic()
            x, y, z, roll, pitch, yaw = pose
            x /= 1000.0
            y /= 1000.0
            z /= 1000.0
            if self.last_pose_ is None:
                self.last_pose_ = np.array([x, y, z, roll, pitch, yaw])
                self.last_time_ = time_stamp
                return
            this_pose = np.array([x, y, z, roll, pitch, yaw])
            new_filtered_pose = self.last_pose_ + self.epow_ * (
                this_pose - self.last_pose_
            )

            self.v_ = (new_filtered_pose[:3] - self.last_pose_[:3]) / (
                time_stamp - self.last_time_
            )
            self.a_ = (new_filtered_pose[3:] - self.last_pose_[3:]) / (
                time_stamp - self.last_time_
            )
            self.a_ = np.arctan2(np.sin(self.a_), np.cos(self.a_))
            R = tf_transformations.euler_matrix(
                new_filtered_pose[3], new_filtered_pose[4], new_filtered_pose[5], "sxyz"
            )[:3, :3]
            self.v_ = np.dot(R.T, self.v_)
            self.a_ = np.dot(R.T, self.a_)
            self.last_pose_ = new_filtered_pose
            self.last_time_ = time_stamp

    def get_latest_pose(self):
        with self.lock_:
            if self.last_pose_ is None:
                return np.zeros(6), 0
            return self.last_pose_, self.last_time_

    def get_latest_velocity(self):
        with self.lock_:
            if self.v_ is None:
                return np.zeros(3), np.zeros(3), 0
            return self.v_, self.a_, self.last_time_

    def get_interpolated_pose(self):
        with self.lock_:
            time_stamp = time.monotonic()
            if self.v_ is None:
                return np.zeros(3), np.zeros(3), time_stamp

            time_diff = time_stamp - self.last_time_

            position = self.last_pose_[:3]
            roll, pitch, yaw = self.last_pose_[3:]
            R = tf_transformations.euler_matrix(roll, pitch, yaw, "sxyz")[:3, :3]
            v_world = np.dot(R, self.v_)
            a_world = np.dot(R, self.a_)
            position += time_diff * v_world
            orientation = self.last_pose_[3:] + time_diff * a_world
            return position, orientation, time_stamp
