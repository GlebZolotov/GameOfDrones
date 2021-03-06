import math
import threading
import queue
import time
import warnings
from argparse import ArgumentParser
from datetime import datetime

import airsimneurips as airsim
import cv2
import numpy as np

warnings.simplefilter("ignore", DeprecationWarning)


# drone_name should match the name in ~/Document/AirSim/settings.json
class BaselineRacer(object):
    def __init__(self, drone_name="drone_1", viz_traj=True, viz_traj_color_rgba=[1.0, 0.0, 0.0, 1.0],
                 viz_image_cv2=True):
        self.drone_name = drone_name
        self.gate_poses_ground_truth = None
        self.viz_image_cv2 = viz_image_cv2
        self.viz_traj = viz_traj
        self.viz_traj_color_rgba = viz_traj_color_rgba
        self.net = cv2.dnn.readNetFromTensorflow(
            "C:\\Users\\User\\PycharmProjects\\AirSim_3.7\\baselines\\net_archs\\ssd_mobilenet_v1\\frozen_inference_graph.pb",
            "C:\\Users\\User\\PycharmProjects\\AirSim_3.7\\baselines\\net_archs\\ssd_mobilenet_v1\\graph.pbtxt"
        )
        self.min_conf = 0.2
        self.in_curse = False
        self.prev_goal = None
        self.time_to_goal = 0
        self.yaw_angle = -1.57
        self.count_im = 0
        self.sum_conf = 0
        self.start_time = 0

        self.airsim_client = airsim.MultirotorClient()
        self.airsim_client.confirmConnection()
        # we need two airsim MultirotorClient objects because the comm lib we use (rpclib) is not thread safe
        # so we poll images in a thread using one airsim MultirotorClient object
        # and use another airsim MultirotorClient for querying state commands 
        self.airsim_client_images = airsim.MultirotorClient()
        self.airsim_client_images.confirmConnection()
        self.airsim_client_odom = airsim.MultirotorClient()
        self.airsim_client_odom.confirmConnection()
        self.level_name = None
        self.stack = queue.LifoQueue()

        self.image_callback_thread = threading.Thread(target=self.repeat_timer_image_callback,
                                                      args=(self.image_callback, 0.05, self.stack))
        self.odometry_callback_thread = threading.Thread(target=self.repeat_timer_odometry_callback,
                                                         args=(self.odometry_callback, 0.02))
        self.is_image_thread_active = False
        self.is_odometry_thread_active = False

        self.MAX_NUMBER_OF_GETOBJECTPOSE_TRIALS = 10  # see https://github.com/microsoft/AirSim-NeurIPS2019-Drone-Racing/issues/38

    # loads desired level
    def load_level(self, level_name, sleep_sec=2.0):
        self.level_name = level_name
        self.airsim_client.simLoadLevel(self.level_name)
        self.airsim_client.confirmConnection()  # failsafe
        time.sleep(sleep_sec)  # let the environment load completely

    # Starts an instance of a race in your given level, if valid
    def start_race(self, tier=3):
        self.airsim_client.simStartRace(tier)

    # Resets a current race: moves players to start positions, timer and penalties reset
    def reset_race(self):
        self.airsim_client.simResetRace()

    # arms drone, enable APIs, set default traj tracker gains
    def initialize_drone(self):
        self.airsim_client.enableApiControl(vehicle_name=self.drone_name)
        self.airsim_client.arm(vehicle_name=self.drone_name)

        # set default values for trajectory tracker gains
        traj_tracker_gains = airsim.TrajectoryTrackerGains(kp_cross_track=5.0, kd_cross_track=0.0,
                                                           kp_vel_cross_track=3.0, kd_vel_cross_track=0.0,
                                                           kp_along_track=0.4, kd_along_track=0.0,
                                                           kp_vel_along_track=0.04, kd_vel_along_track=0.0,
                                                           kp_z_track=2.0, kd_z_track=0.0,
                                                           kp_vel_z=0.4, kd_vel_z=0.0,
                                                           kp_yaw=3.0, kd_yaw=0.1)

        self.airsim_client.setTrajectoryTrackerGains(traj_tracker_gains, vehicle_name=self.drone_name)
        self.airsim_client
        time.sleep(0.2)

    def takeoffAsync(self):
        self.airsim_client.takeoffAsync().join()

    # like takeoffAsync(), but with moveOnSpline()
    def takeoff_with_moveOnSpline(self, takeoff_height=1.0):
        start_position = self.airsim_client.simGetVehiclePose(vehicle_name=self.drone_name).position
        takeoff_waypoint = airsim.Vector3r(start_position.x_val, start_position.y_val,
                                           start_position.z_val - takeoff_height)

        self.airsim_client.moveOnSplineAsync([takeoff_waypoint], vel_max=15.0, acc_max=5.0,
                                             add_position_constraint=True, add_velocity_constraint=False,
                                             add_acceleration_constraint=False, viz_traj=self.viz_traj,
                                             viz_traj_color_rgba=self.viz_traj_color_rgba,
                                             vehicle_name=self.drone_name).join()

    # stores gate ground truth poses as a list of airsim.Pose() objects in self.gate_poses_ground_truth
    def get_ground_truth_gate_poses(self):
        gate_names_sorted_bad = sorted(self.airsim_client.simListSceneObjects("Gate.*"))
        # gate_names_sorted_bad is of the form `GateN_GARBAGE`. for example:
        # ['Gate0', 'Gate10_21', 'Gate11_23', 'Gate1_3', 'Gate2_5', 'Gate3_7', 'Gate4_9', 'Gate5_11', 'Gate6_13', 'Gate7_15', 'Gate8_17', 'Gate9_19']
        # we sort them by their ibdex of occurence along the race track(N), and ignore the unreal garbage number after the underscore(GARBAGE)
        gate_indices_bad = [int(gate_name.split('_')[0][4:]) for gate_name in gate_names_sorted_bad]
        gate_indices_correct = sorted(range(len(gate_indices_bad)), key=lambda k: gate_indices_bad[k])
        gate_names_sorted = [gate_names_sorted_bad[gate_idx] for gate_idx in gate_indices_correct]
        self.gate_poses_ground_truth = []
        for gate_name in gate_names_sorted:
            curr_pose = self.airsim_client.simGetObjectPose(gate_name)
            counter = 0
            while (math.isnan(curr_pose.position.x_val) or math.isnan(curr_pose.position.y_val) or math.isnan(
                    curr_pose.position.z_val)) and (counter < self.MAX_NUMBER_OF_GETOBJECTPOSE_TRIALS):
                print(f"DEBUG: {gate_name} position is nan, retrying...")
                counter += 1
                curr_pose = self.airsim_client.simGetObjectPose(gate_name)
            assert not math.isnan(
                curr_pose.position.x_val), f"ERROR: {gate_name} curr_pose.position.x_val is still {curr_pose.position.x_val} after {counter} trials"
            assert not math.isnan(
                curr_pose.position.y_val), f"ERROR: {gate_name} curr_pose.position.y_val is still {curr_pose.position.y_val} after {counter} trials"
            assert not math.isnan(
                curr_pose.position.z_val), f"ERROR: {gate_name} curr_pose.position.z_val is still {curr_pose.position.z_val} after {counter} trials"
            self.gate_poses_ground_truth.append(curr_pose)

    # this is utility function to get a velocity constraint which can be passed to moveOnSplineVelConstraints() 
    # the "scale" parameter scales the gate facing vector accordingly, thereby dictating the speed of the velocity constraint
    def get_gate_facing_vector_from_quaternion(self, airsim_quat, scale=1.0):
        import numpy as np
        # convert gate quaternion to rotation matrix. 
        # ref: https://en.wikipedia.org/wiki/Rotation_matrix#Quaternion; https://www.lfd.uci.edu/~gohlke/code/transformations.py.html
        q = np.array([airsim_quat.w_val, airsim_quat.x_val, airsim_quat.y_val, airsim_quat.z_val], dtype=np.float64)
        n = np.dot(q, q)
        if n < np.finfo(float).eps:
            return airsim.Vector3r(0.0, 1.0, 0.0)
        q *= np.sqrt(2.0 / n)
        q = np.outer(q, q)
        rotation_matrix = np.array([[1.0 - q[2, 2] - q[3, 3], q[1, 2] - q[3, 0], q[1, 3] + q[2, 0]],
                                    [q[1, 2] + q[3, 0], 1.0 - q[1, 1] - q[3, 3], q[2, 3] - q[1, 0]],
                                    [q[1, 3] - q[2, 0], q[2, 3] + q[1, 0], 1.0 - q[1, 1] - q[2, 2]]])
        gate_facing_vector = rotation_matrix[:, 1]
        return airsim.Vector3r(scale * gate_facing_vector[0], scale * gate_facing_vector[1],
                               scale * gate_facing_vector[2])

    def fly_through_all_gates_one_by_one_with_moveOnSpline(self):
        if self.level_name == "Building99_Hard":
            vel_max = 5.0
            acc_max = 2.0

        if self.level_name in ["Soccer_Field_Medium", "Soccer_Field_Easy", "ZhangJiaJie_Medium"]:
            vel_max = 10.0
            acc_max = 5.0

        return self.airsim_client.moveOnSplineAsync([gate_pose.position for gate_pose in self.gate_poses_ground_truth],
                                                    vel_max=vel_max, acc_max=acc_max,
                                                    add_position_constraint=True, add_velocity_constraint=False,
                                                    add_acceleration_constraint=False, viz_traj=self.viz_traj,
                                                    viz_traj_color_rgba=self.viz_traj_color_rgba,
                                                    vehicle_name=self.drone_name)

    def fly_through_all_gates_at_once_with_moveOnSpline(self):
        if self.level_name in ["Soccer_Field_Medium", "Soccer_Field_Easy", "ZhangJiaJie_Medium", "Qualifier_Tier_1",
                               "Qualifier_Tier_2", "Qualifier_Tier_3", "Final_Tier_1", "Final_Tier_2", "Final_Tier_3"]:
            vel_max = 30.0
            acc_max = 15.0

        if self.level_name == "Building99_Hard":
            vel_max = 4.0
            acc_max = 1.0

        return self.airsim_client.moveOnSplineAsync([gate_pose.position for gate_pose in self.gate_poses_ground_truth],
                                                    vel_max=vel_max, acc_max=acc_max,
                                                    add_position_constraint=True, add_velocity_constraint=False,
                                                    add_acceleration_constraint=False, viz_traj=self.viz_traj,
                                                    viz_traj_color_rgba=self.viz_traj_color_rgba,
                                                    vehicle_name=self.drone_name)

    def fly_through_all_gates_one_by_one_with_moveOnSplineVelConstraints(self):
        add_velocity_constraint = True
        add_acceleration_constraint = False

        if self.level_name in ["Soccer_Field_Medium", "Soccer_Field_Easy"]:
            vel_max = 15.0
            acc_max = 3.0
            speed_through_gate = 2.5

        if self.level_name == "ZhangJiaJie_Medium":
            vel_max = 10.0
            acc_max = 3.0
            speed_through_gate = 1.0

        if self.level_name == "Building99_Hard":
            vel_max = 2.0
            acc_max = 0.5
            speed_through_gate = 0.5
            add_velocity_constraint = False

        # scale param scales the gate facing vector by desired speed. 
        return self.airsim_client.moveOnSplineVelConstraintsAsync(
            [gate_pose.position for gate_pose in self.gate_poses_ground_truth],
            [self.get_gate_facing_vector_from_quaternion(gate_pose.orientation, scale=speed_through_gate) for gate_pose
             in self.gate_poses_ground_truth],
            vel_max=vel_max, acc_max=acc_max,
            add_position_constraint=True, add_velocity_constraint=add_velocity_constraint,
            add_acceleration_constraint=add_acceleration_constraint,
            viz_traj=self.viz_traj, viz_traj_color_rgba=self.viz_traj_color_rgba, vehicle_name=self.drone_name)

    def fly_through_all_gates_at_once_with_moveOnSplineVelConstraints(self):
        if self.level_name in ["Soccer_Field_Easy", "Soccer_Field_Medium", "ZhangJiaJie_Medium"]:
            vel_max = 15.0
            acc_max = 7.5
            speed_through_gate = 2.5

        if self.level_name == "Building99_Hard":
            vel_max = 5.0
            acc_max = 2.0
            speed_through_gate = 1.0

        return self.airsim_client.moveOnSplineVelConstraintsAsync(
            [gate_pose.position for gate_pose in self.gate_poses_ground_truth],
            [self.get_gate_facing_vector_from_quaternion(gate_pose.orientation, scale=speed_through_gate) for gate_pose
             in self.gate_poses_ground_truth],
            vel_max=vel_max, acc_max=acc_max,
            add_position_constraint=True, add_velocity_constraint=True, add_acceleration_constraint=False,
            viz_traj=self.viz_traj, viz_traj_color_rgba=self.viz_traj_color_rgba, vehicle_name=self.drone_name)

    def get_nearest_frame_center(self, i__=None):
        class_obj = "gate"
        color_obj = (240, 240, 60)
        cv_img = self.stack.get()
        self.stack.task_done()
        frame = cv2.resize(cv_img, (300, 300), interpolation=cv2.INTER_AREA)
        (h, w) = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(frame, size=(300, 300), swapRB=True)
        self.net.setInput(blob)
        detections = self.net.forward()
        gate_centers = []
        for i in np.arange(0, detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence > self.min_conf:
                # idx = int(detections[0, 0, i, 1])
                box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
                (startX, startY, endX, endY) = box.astype("int")
                gate_centers.append(((endX - startX) * (endY - startY),
                                     (startX + endX) // 2 - 150,
                                     (startY + endY) // 2 - 150,
                                     confidence,
                                     max((endX - startX)/300, (endY - startY)/300)))
                label = "{}: {:.2f}%".format(class_obj, confidence * 100)
                cv2.rectangle(frame, (startX, startY), (endX, endY), color_obj, 2)
                y = startY - 15 if startY - 15 > 15 else startY + 15
                cv2.putText(frame, label, (startX, y + 3), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color_obj, 1)
        cv2.imshow("img_rgb", frame)
        cv2.waitKey(1)
        if i__ is not None:
            cv2.imwrite("imgs\\" + str(i__) + ".jpg", frame)
        if len(gate_centers) > 0:
            gate_centers.sort(key=lambda center: center[0])
            return gate_centers[-1][1], gate_centers[-1][2], gate_centers[-1][3], gate_centers[-1][4]
        return None

    def fly_itself(self):
        i__ = 0
        while True:  # i__ < 50:
            if not self.stack.empty():
                if self.time_to_goal > time.perf_counter():
                    self.airsim_client.moveByRollPitchYawThrottleAsync(0,
                                                                       0.5,
                                                                       -1.57,
                                                                       0.6, 0.1,
                                                                       self.drone_name)
                    continue
                near_center = self.get_nearest_frame_center()
                i__ += 1
                velocities = self.airsim_client.getMultirotorState(self.drone_name).kinematics_estimated.linear_velocity
                cur_vel = math.sqrt(velocities.x_val ** 2 + velocities.y_val ** 2)
                if near_center is not None:
                    self.prev_goal = near_center
                    if math.fabs(self.prev_goal[0]) < 0 and math.fabs(self.prev_goal[1]) < 0:
                        self.time_to_goal = time.perf_counter() + 5
                    self.yaw_angle -= self.prev_goal[0] * math.log((cur_vel + 1) * math.e) * self.prev_goal[3] / 300
                    self.set_trajectory(cur_vel)
                elif self.prev_goal is not None:
                    self.set_trajectory(cur_vel)
            else:
                time.sleep(0.15)

    def set_trajectory(self, cur_vel):
        # print(cur_vel)
        if cur_vel < 3:
            self.airsim_client.moveByRollPitchYawZAsync(self.prev_goal[0] * math.log((cur_vel + 1) * math.e) * self.prev_goal[3] / 500,
                                                        0.03,
                                                        self.yaw_angle,
                                                        self.airsim_client.simGetVehiclePose(
                                                            self.drone_name).position.z_val +
                                                        self.prev_goal[1] / 50, 0.5,
                                                        self.drone_name)
        else:
            self.airsim_client.moveByRollPitchYawZAsync(self.prev_goal[0] * math.log((cur_vel + 1) * math.e) * self.prev_goal[3] / 500,
                                                        0.0,
                                                        self.yaw_angle,
                                                        self.airsim_client.simGetVehiclePose(
                                                            self.drone_name).position.z_val +
                                                        self.prev_goal[1] / 50, 0.5,
                                                        self.drone_name)

    def image_callback(self, stack):
        # get uncompressed fpv cam image
        request = [airsim.ImageRequest("fpv_cam", airsim.ImageType.Scene, False, False)]
        response = self.airsim_client_images.simGetImages(request)
        time.sleep(0.1)
        img_rgb_1d = np.fromstring(response[0].image_data_uint8, dtype=np.uint8)
        img_rgb = img_rgb_1d.reshape(response[0].height, response[0].width, 3)
        if self.start_time == 0:
            self.start_time = datetime.now()
        print(datetime.now() - self.start_time)
        drone_state = self.airsim_client_images.getMultirotorState()
        # in world frame:
        print(drone_state.kinematics_estimated.position)

        if stack.qsize() > 5:
            while not stack.empty():
                try:
                    stack.get(False)
                except queue.Empty:
                    continue
                stack.task_done()
        stack.put(img_rgb)
        # if self.viz_image_cv2:
        #     cv2.imshow("img_rgb", img_rgb)
        # cv2.waitKey(1)

        # near_center = self.get_nearest_frame_center(self.count_im)
        # self.sum_conf += near_center[2] if near_center is not None else 0
        # self.count_im += 1
        # print(self.sum_conf / self.count_im)
        # print(time.perf_counter())

    def odometry_callback(self):
        # get uncompressed fpv cam image
        drone_state = self.airsim_client_odom.getMultirotorState()
        # in world frame:
        position = drone_state.kinematics_estimated.position
        orientation = drone_state.kinematics_estimated.orientation
        linear_velocity = drone_state.kinematics_estimated.linear_velocity
        angular_velocity = drone_state.kinematics_estimated.angular_velocity

    # call task() method every "period" seconds. 
    def repeat_timer_image_callback(self, task, period, stack):
        while self.is_image_thread_active:
            task(stack)
            time.sleep(period)

    def repeat_timer_odometry_callback(self, task, period):
        while self.is_odometry_thread_active:
            task()
            time.sleep(period)

    def start_image_callback_thread(self):
        if not self.is_image_thread_active:
            self.is_image_thread_active = True
            self.image_callback_thread.start()
            print("Started image callback thread")

    def stop_image_callback_thread(self):
        if self.is_image_thread_active:
            self.is_image_thread_active = False
            self.image_callback_thread.join()
            print("Stopped image callback thread.")

    def start_odometry_callback_thread(self):
        if not self.is_odometry_thread_active:
            self.is_odometry_thread_active = True
            self.odometry_callback_thread.start()
            print("Started odometry callback thread")

    def stop_odometry_callback_thread(self):
        if self.is_odometry_thread_active:
            self.is_odometry_thread_active = False
            self.odometry_callback_thread.join()
            print("Stopped odometry callback thread.")


def main(args):
    # ensure you have generated the neurips planning settings file by running python generate_settings_file.py
    baseline_racer = BaselineRacer(drone_name="drone_1", viz_traj=args.viz_traj,
                                   viz_traj_color_rgba=[1.0, 1.0, 0.0, 1.0], viz_image_cv2=args.viz_image_cv2)
    baseline_racer.load_level(args.level_name)
    if args.level_name == "Qualifier_Tier_1":
        args.race_tier = 1
    if args.level_name == "Qualifier_Tier_2":
        args.race_tier = 2
    if args.level_name == "Qualifier_Tier_3":
        args.race_tier = 3
    baseline_racer.start_race(args.race_tier)
    baseline_racer.initialize_drone()
    baseline_racer.takeoff_with_moveOnSpline()
    # baseline_racer.takeoffAsync()
    baseline_racer.get_ground_truth_gate_poses()
    baseline_racer.start_image_callback_thread()
    baseline_racer.start_odometry_callback_thread()

    if args.planning_baseline_type == "all_gates_at_once":
        if args.planning_and_control_api == "moveOnSpline":
            baseline_racer.fly_through_all_gates_at_once_with_moveOnSpline().join()
        if args.planning_and_control_api == "moveOnSplineVelConstraints":
            baseline_racer.fly_through_all_gates_at_once_with_moveOnSplineVelConstraints().join()
        if args.planning_and_control_api == "my":
            baseline_racer.fly_itself()

    if args.planning_baseline_type == "all_gates_one_by_one":
        if args.planning_and_control_api == "moveOnSpline":
            baseline_racer.fly_through_all_gates_one_by_one_with_moveOnSpline().join()
        if args.planning_and_control_api == "moveOnSplineVelConstraints":
            baseline_racer.fly_through_all_gates_one_by_one_with_moveOnSplineVelConstraints().join()

    # Comment out the following if you observe the python script exiting prematurely, and resetting the race 
    baseline_racer.stop_image_callback_thread()
    baseline_racer.stop_odometry_callback_thread()
    baseline_racer.reset_race()


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument('--level_name', type=str,
                        choices=["Soccer_Field_Easy", "Soccer_Field_Medium", "ZhangJiaJie_Medium", "Building99_Hard",
                                 "Qualifier_Tier_1", "Qualifier_Tier_2", "Qualifier_Tier_3", "Final_Tier_1",
                                 "Final_Tier_2", "Final_Tier_3"], default="ZhangJiaJie_Medium")
    parser.add_argument('--planning_baseline_type', type=str, choices=["all_gates_at_once", "all_gates_one_by_one"],
                        default="all_gates_at_once")
    parser.add_argument('--planning_and_control_api', type=str,
                        choices=["moveOnSpline", "moveOnSplineVelConstraints", "my"],
                        default="moveOnSpline")
    parser.add_argument('--enable_viz_traj', dest='viz_traj', action='store_true', default=False)
    parser.add_argument('--enable_viz_image_cv2', dest='viz_image_cv2', action='store_true', default=False)
    parser.add_argument('--race_tier', type=int, choices=[1, 2, 3], default=1)
    args = parser.parse_args()
    main(args)
