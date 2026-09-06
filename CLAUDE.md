# vicon_interface

ROS 2 (`ament_python`) driver for the Vicon Datastream SDK. Polls the tracker and publishes,
per tracked object: `vicon_pose/<obj>`, `odometry/<obj>`, `odometry/filtered/<obj>`,
`vicon_latency/<obj>`, and TF. See `README.md` for install and `AGENTS.md` for repo
conventions.

Downstream consumer today: `humanoid-control`'s `whole_body_inference`, whose `_gt` policy
variants observe the base's linear velocity and height.

## Frame offsets — the one design decision worth knowing

`child_frame_offset_xyz` / `child_frame_offset_rpy_deg` express the pose of the frame to
report, in the tracked rigid body's own frame (`T_marker->child`). They are applied
**per sample inside `ViconDataBuffer.add_pose()`, upstream of the finite difference**.

That placement is the whole point, not an implementation detail. Differentiating the
*corrected* position reproduces the lever-arm term for free:

```
p_child(t) = p_marker(t) + R(t)·d
d/dt       = v_marker + Ṙ·d = v_marker + ω × (R·d)
```

So the published twist is the child frame's velocity with no angular rate needed anywhere.
Correcting *after* the differencing — which is what an offset applied downstream would do —
fixes position but leaves the velocity wrong by `ω × r`. Measured on the real buffer with a
0.25 m arm and a 10° / 1.4 Hz pitch: 0.384 m/s of false velocity, larger than the robot's
own 0.3 m/s forward speed. **Do not move this compensation downstream.**

Current values are for `arcadian`: its Vicon rigid body is defined 0.25 m *above* base_link,
so base_link sits 0.25 m below it in the rigid body's frame and the z entry is **negative**.
The rotation is zero only because the rigid body axes happen to align with base_link; if a
rigid body is ever redefined rotated, put the correction in `child_frame_offset_rpy_deg`
rather than anywhere downstream.

Verified to machine precision (position, body-frame velocity and orientation) against
analytic truth for zero, yaw-90°, and 10/-20/45° rotation offsets. With both offsets zero the
node's output is bit-identical to the pre-offset implementation.

Caveat: a rotation offset large enough to put the reported pitch near ±90° degrades the Euler
representation, and `twist.angular` — which is an Euler-angle rate, not a true ω, and always
has been — would distort with it.

## Two things that bite consumers

- `odometry/<obj>` now means the **child frame** (base_link), not the marker. Bags and
  analysis scripts written against the old meaning need updating.
- `odometry/<obj>` back-dates `header.stamp` by the Vicon pipeline latency, so
  `now - stamp` is a real end-to-end delay and latency compensation works.
  `odometry/filtered/<obj>` stamps with `now()` instead — subscribing to it silently makes
  the measured delay zero. Consumers that compensate for latency must use the unfiltered one.
- Velocity is a finite difference across the whole rolling window (`rolling_window_size`,
  5 in the param file at 250 Hz), so it carries ~8 ms of group delay relative to its own
  timestamp. Unrelated to the offset work; still present.
