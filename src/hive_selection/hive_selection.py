# Ra-Ya imports
from raya.controllers.cameras_controller import CamerasController
from raya.controllers.cv_controller import CVController
from raya.controllers.arms_controller import ArmsController
from raya.controllers.navigation_controller import NavigationController
from raya.controllers.lidar_controller import LidarController
from raya.controllers.motion_controller import MotionController
from raya.controllers.sound_controller import SoundController
from raya.controllers.navigation_controller import POSITION_UNIT, ANGLE_UNIT
from gary_arms_msgs.action import CalibrateGripper
from raya.skills import RayaFSMSkill
from skills.approach_to_tags import SkillApproachToTags
from raya.tools.image import show_image, draw_on_image

# Filesystem imports
from skills.hive_selection.constants import *
from skills.hive_selection.arms import *
from skills.hive_selection.navigation import *

# Other imports
import asyncio
import argparse
import time
import cv2
import numpy as np

class SkillHiveSelection(RayaFSMSkill):

    ###------------------------------ SKILL ------------------------------###

    REQUIRED_SETUP_ARGS = [
        'working_camera',
        'map_name',
        'item_name',
        'tag_size',
    ]
    
    DEFAULT_SETUP_ARGS = {
        'fsm_log_transitions': True,
        'identifier': 'blablabla',
        'arm_name' : 'right_arm',
        'tag_families' : ['tag36h11.43','tag36h11.1']
    }

    REQUIRED_EXECUTE_ARGS = [
        'angle_to_goal'
    ]

    DEFAULT_EXECUTE_ARGS = {
        'distance_to_goal' : 0.75
    }


    ###------------------------------ FSM ------------------------------###

    STATES = [
        'DEBUG_STATE',
        'NAVIGATING_TO_CART',
        'APPROACHING_CART',
        'DETECTING_TAGS_1',
        'MOVING_SIDEWAYS',
        'DETECTING_TAGS_2',
        'POSITION_ARM',
        'PICK_ITEM',
        'END'
    ]

    INITIAL_STATE = 'NAVIGATING_TO_CART'

    END_STATES = [
        'END'
    ]
    
    STATES_TIMEOUTS = {'DETECTING_TAGS_1' :
                       (NO_TARGET_TIMEOUT, ERROR_TAG_NOT_FOUND)}

    debug = True
    if debug is True:
        INITIAL_STATE = 'DEBUG_STATE'

    ###--------------------------- SKILL METHODS ---------------------------###

    async def setup(self):
        
        # Setup variables
        self.setup_variables()

        # Get controllers
        self.cameras = await self.get_controller('cameras')
        self.log.info('Cameras controller - Enabled')
        self.cv = await self.get_controller('cv')
        self.log.info('CV controller - Enabled')
        self.navigation = await self.get_controller('navigation')
        self.log.info('Navigation controller - Enabled')
        self.motion = await self.get_controller('motion')
        self.log.info('Motion controller - Enabled')
        self.lidar = await self.get_controller('lidar')
        self.log.info('Lidar controller - Enabled')
        self.arms = await self.get_controller('arms')
        self.log.info('Arms controller - Enabled')
        self.sound = await self.get_controller('sound')
        self.log.info('Sound controller - Enabled')

        # Enable camera
        self.log.info(f"Enabling camera {self.setup_args['working_camera']}...")
        await self.cameras.enable_color_camera(self.setup_args['working_camera'])
        
        # Set map
        self.log.info(f"Localizing in map: {self.setup_args['map_name']}...")
        await self.navigation.set_map(
            map_name = self.setup_args['map_name'],
            wait_localization = True,
            wait = True
        )

        # Resgister approach skill
        self.log.info('Registering Helper Skill: ApproachToTags...')
        self.skill_approach = self.register_skill(SkillApproachToTags)

        # Setup done log
        self.log.info('Setup Done!')


    async def finish(self):
        pass


    ###------------------------------ HELPERS ------------------------------###

    def setup_variables(self):
        '''Setup initial variables'''

        # General variables
        self.convertion_dict = {'water bottle' : 1, # Dict to convert item name
                                'towel' : 4,        # to apriltag number
                                'pajamas' : 3}
        self.tag = self.convertion_dict[self.setup_args['item_name']] # Tag number
        self.navigation_successful = False  # Navigation success flag
        self.approach_successful = False    # Approach success flag
        self.tag_x = None                   # x of the tag (from baselink)
        self.tag_y = None                   # y of the tag (from baselink)
        self.tag_z = None                   # z of the tag (from baselink)
        self.navigation_counter = 0         # Navigation attempts counter
        self.position_attempts = 0          # Arm position attempts counter
        self.approach_final_linear = 0      # Approach final linear step
        self.approach_angle_error = 0       # Approach final angle error
        self.approach_counter = 0           # Counter for the approach attempts
        self.sideways_distance = 0          # Sideways distance to move
        self.detections_dict = {}           # Dictionary to store detections
        self.tags_detected = False          # Flag whether tags are detected
        self.tags_info = self.create_dict_arg(self.setup_args['tag_families'])


        # Arms variables
        self.arm_name = self.setup_args['arm_name']
        self.joint_names = JOINT_NAMES



    def reset_approach_feedbacks(self):
        '''Reset the feedbacks from the approach skill'''
        self.approach_successful = False
    


    async def check_approach_success(self, thresh, max_attempts):
        '''
        INPUTS:
            thresh - the distance under which the robot is close enough (meters)
            max_attempts - the maximum times to try to get closer

        OUTPUTS:
            The function changes the self.approach_successful flag
        '''

        # Get the min distance read from the lidar
        raw_lidar_data = self.lidar.get_raw_data()
        raw_front_data = raw_lidar_data[-10:] + raw_lidar_data[:10]
        min_scan_distance = min(raw_front_data)
        min_actual_distance = min_scan_distance - thresh

        # If you're close enough, return
        if min_actual_distance <= thresh:
            self.approach_successful = True
            return

        # Otherwise, try to move forwards  max_attempts
        else:
            await self.motion.move_linear(distance = min_actual_distance,
                                            x_velocity = 0.05)
            raw_lidar_data = self.lidar.get_raw_data()
            min_scan_distance = min(raw_lidar_data)
            min_actual_distance = min_scan_distance - thresh

            if min_actual_distance <= thresh:
                self.approach_successful = True
                return

            self.approach_counter += 1
                
            
        self.approach_successful = False


    async def gripper_command(self, command):
        """Opens/closes both grippers"""
        try:
            self.log.info(f'Gripper command \'{command}\'...')
            await self.arms.gripper_cmd(
                **(GRIPPER_COMMANDS[command]),
                wait=True,
            )
        except Exception as e:
            print(e)
        await self.sleep(2)



    async def calibrate_gripper(self, arm):
        """Calibrates gripper on a given arm"""
        # pass
        goal = CalibrateGripper.Goal()
        goal.hand = arm  # side = "right_arm"/"left_arm"
        print(f'calibrating {arm}')
        self.__cli__calibrate_gripper.wait_for_server()
        result = await self.__cli__calibrate_gripper.send_goal_async(goal)
        print(f'result:{result}')
        print(f'done calibrating {arm}')



    async def forward_kinematics(self,
                                 pose,
                                 cartesian_path = True,
                                 planner = 'RRTconnect',
                                 units = ANGLE_UNIT.DEGREES):
        '''
            INPUTS:
                pose: dict with keys of x, y, z, roll, pitch, yaw, and float
                      values

            OUTPUTS:
                The function executes forward kinematics to the desired location
        '''
        
        await self.arms.set_pose(
            arm=self.arm_name,
            x = pose["x"],
            y = pose["y"],
            z = pose["z"],
            roll = pose["roll"],
            pitch = pose["pitch"],
            yaw = pose["yaw"],
            units = units,
            cartesian_path = cartesian_path,
            callback_feedback = self.arms_callback_feedback,
            callback_finish = self.arms_callback_finish,
            velocity_scaling = 0.1,
            acceleration_scaling = 0.1,
            wait = True,
            additional_options = {'planner' : planner}
        )



    async def return_arm_home(self):
        await self.arms.set_predefined_pose(
                                arm = self.arm_name,
                                predefined_pose = 'home',
                                callback_feedback = self.arms_callback_feedback,
                                wait = True)

        await self.gripper_command('open')



    async def turn_and_burn(self, distance):
        '''Turn 90 degrees, move forwards, turn back'''
        await self.motion.rotate(angle = 90,
                                 angular_speed = 15,
                                 wait = True)
        
        await self.motion.move_linear(distance = distance,
                                      x_velocity = 0.05,
                                      wait = True)

        await self.motion.rotate(angle = -90,
                                 angular_speed = 15,
                                 wait = True)



    async def static_trex_position(self):
        '''Position arm in trex position'''
        await self.arms.set_joints_position(
            arm=self.arm_name,
            name_joints=self.joint_names,
            angle_joints = TREX_POSITION_ANGLES,
            units = ANGLE_UNIT.RADIANS,
            use_obstacles = True,
            save_trajectory = True,
            name_trajectory = 'trex_position',
            velocity_scaling = 0.4,
            acceleration_scaling =  0.4,
            wait=True)

        self.static_trex_pose = await self.arms.get_current_pose(self.arm_name)
        self.trex_position = self.static_trex_pose['position']



    async def dynamic_trex_position(self):
        '''Position arm in trex position'''
        self.trex_pose = {
            'x' : self.tag_x + RIGHT_ARM_OFFSET['x'] - 0.1,
            'y' : self.tag_y + RIGHT_ARM_OFFSET['y'],
            'z' : self.tag_z + RIGHT_ARM_OFFSET['z'] + 0.08,
            'roll' : 0,
            'pitch' : 0,
            'yaw' : 0
        }
        
        self.trex_position = [self.trex_pose['x'],
                              self.trex_pose['y'],
                              self.trex_pose['z']]


        await self.forward_kinematics(self.trex_pose,
                                      #cartesian_path = True,
                                      planner = 'RRTconnect')
    

    def pixels2meters(self):
        '''Calculate the distance to move sideways from the console'''
        if self.tag in self.detections_dict:

            # Convert camera pixels to meters in the current position
            #!!left/right in this position is the X axis for the camera but
            # the Y axis for the base link axes system, hence the names below!!
            x_cam_detection_pix = self.detections_dict[self.tag]['object_center_px'][1]
            x_cam_center_dist_pix = abs(MAX_CAMERA_PIXELS_X/2 - x_cam_detection_pix)
            x_cam_edge_dist_pix = abs(MAX_CAMERA_PIXELS_X - x_cam_detection_pix)
            y_base_dist_meters = self.detections_dict[self.tag]['center_point'][1]
            pix_meters_ratio = abs(y_base_dist_meters / x_cam_center_dist_pix)

            # Distance so that the tag would be on the edge of the screen
            side_linear = float(x_cam_edge_dist_pix * pix_meters_ratio)

            # Semi automatic correction in case of inaccuracies
            if side_linear > 0.4  or side_linear < 0.275:
                side_linear =  0.32 + y_base_dist_meters
            
                return side_linear



    async def check_navigation_success(self):
        robot_meter_deg = await self.navigation.get_position(
                                                pos_unit=POSITION_UNIT.METERS,
                                                ang_unit=ANGLE_UNIT.DEGREES)
        
        if np.sqrt(((robot_meter_deg[0]) - NAV_POINT_CART['x'])**2 + \
            (robot_meter_deg[1] - NAV_POINT_CART['y'])**2) <= NAVIGATION_THRESHOLD:
            self.navigation_successful = True

        else:
            self.navigation_successful = False
    


    def reset_detections(self):
        '''Reset the detections'''
        self.tags_detected = False
        self.detections_dict = {}
        self.tag_x, self.tag_y, self.tag_z = None, None, None


    def create_dict_arg(self, arg_list):
        dict_r = {}
        for dt in arg_list:
            if dt.split('.')[0] in dict_r:
                dict_r[dt.split('.')[0]].append(int(dt.split('.')[1]))   
            else: 
                dict_r[dt.split('.')[0]] = [int(dt.split('.')[1])]

        return dict_r

    ###----------------------------- CALLBACKS -----------------------------###

    async def skill_callback_feedback(self, feedback):
        '''ApproachToSomething skill feedback callback'''
        self.log.info(f'approach feedback: {feedback}')
        if 'final_linear' in feedback:
            self.approach_final_linear = feedback['final_linear']



    async def skill_callback_done(self, done_feedback, done_info):
        '''ApproachToSomething skill finish callback'''
        self.approach_done_feedback = done_feedback
        self.log.info(f'approach done feedback: {done_feedback}')
        self.log.info(f'approach done info: {done_info}')
        # if 'final_error_angle' in done_info:
        #     self.approach_angle_error = done_info['final_error_angle']



    def arms_callback_feedback(self, code, error_feedback, arm, percentage):
        self.log.info(f'ARM: {arm} TRAJECTORY: {percentage:.2f}% DONE')



    def arms_callback_finish(self, error, error_msg, fraction):
        self.log.info('')
        if error == 0:
            self.log.info(
                f'FINISH SUCCESSFULLY THE EXECUTION OF THE POSE')
        else:
            self.log.error(
                f'ERROR IN THE EXECUTION NUMBER: {error}:{error_msg}')



    def callback_predictions(self, predictions, image):
        '''Callback used to obtain predictions'''
        self.image = image
        if predictions:
            for pred in predictions:
                tag_id = pred['tag_id']
                self.detections_dict[tag_id] = pred

            if self.tag in self.detections_dict:
                self.tag_x = self.detections_dict[self.tag]['pose_base_link'].pose.position.x
                self.tag_y = self.detections_dict[self.tag]['pose_base_link'].pose.position.y
                self.tag_z = self.detections_dict[self.tag]['pose_base_link'].pose.position.z
                self.tags_detected = True



    def callback_specific_tags(self, detected_tag, tag_info, timestamp):
        self.log.info(f'!!!!')
        self.log.info(f'Tag {detected_tag} detected')
        self.log.info(f'Tag info type: {type(tag_info)}')
        self.log.info(f'Tag timestamp {timestamp}')
        self.log.info(f'!!!!')



    ###------------------------------ ACTIONS ------------------------------###

    async def enter_NAVIGATING_TO_CART(self):
        '''Action used to navigate to the cart'''
        await self.navigation.navigate_to_position(x = NAV_POINT_CART['x'],
                                                   y = NAV_POINT_CART['y'],
                                                   angle = self.execute_args['angle_to_goal'],
                                                   pos_unit = POSITION_UNIT.METERS,
                                                   ang_unit = ANGLE_UNIT.DEGREES,
                                                   wait = True)



    async def enter_APPROACHING_CART(self):
        '''Action used to execute the approach skill'''

        self.approach_successful = False
        self.log.info('Executing ApproachToTags skill...')
        await self.skill_approach.execute_setup(
             setup_args = {
                'tags_size' : self.setup_args['tag_size'],
                'working_cameras' : self.setup_args['working_camera'],

                'angle_to_goal' : self.execute_args['angle_to_goal'],
                'distance_to_goal': self.execute_args['distance_to_goal'],
                'linear_velocity': 0.06,
                'max_x_error_allowed': 0.03,
                'max_y_error_allowed': 0.02,
                'max_angle_error_allowed' : 3.0,
                'min_correction_distance': 0.1,
            },
            # setup_args = {
            #     'working_camera' : self.setup_args['working_camera'],
            #     'map_name': self.setup_args['map_name'],
            #     'predictor' : 'apriltags',
            #     'identifier': self.setup_args['identifier']
            # }


        )

        await self.skill_approach.execute_main(
            # execute_args = {
            #     'angle_to_goal' : self.execute_args['angle_to_goal'],
            #     'distance_to_goal': self.execute_args['distance_to_goal'],
            #     'linear_velocity': 0.06,
            #     'max_x_error_allowed': 0.03,
            #     'max_y_error_allowed': 0.02,
            #     'max_angle_error_allowed' : 3.0,
            #     'min_correction_distance': 0.1,
            # },
            wait = False,
            callback_feedback = self.skill_callback_feedback,
            callback_done = self.skill_callback_done
        )

        await self.skill_approach.wait_main()
        await self.skill_approach.execute_finish()
        await self.check_approach_success(
                thresh = self.execute_args['distance_to_goal'] + 0.1,
                max_attempts = 3
            )


    async def enter_DETECTING_TAGS_1(self):
         # Enable model
        self.log.info('Enabling apriltags model...')

        self.predictor_handler = await self.cv.enable_model(
                model = 'detector',type = 'tag',
                name = 'apriltags', 
                source = self.setup_args['working_camera'],
                model_params = {
                'families' : 'tag36h11',
                'nthreads' : 4,
                'quad_decimate' : 2.0,
                'quad_sigma': 0.0,
                'decode_sharpening' : 0.25,
                'refine_edges' : 1,
                'tag_size' : self.setup_args['tag_size']
                }
            )
        
         # Create listeners
        await self.predictor_handler.find_tags(
                tags = self.tags_info, 
                callback = self.callback_specific_tags
            )
        
        self.predictor_handler.set_img_detections_callback(
                callback = self.callback_predictions,
                as_dict = True,
                call_without_detections = True,
                cameras_controller = self.cameras
            )

        # Start timer
        self.detection_start_time = time.time()



    async def enter_MOVING_SIDEWAYS(self):
        await self.sleep(1.5)
        await self.turn_and_burn(self.sideways_distance)

    

    async def enter_DETECTING_TAGS_2(self):
        # Start timer (model is already enabled)
        await self.sleep(1.5)
        self.detection_start_time = time.time()



    async def enter_POSITION_ARM(self):
        '''Action used to position the arm before grabbing the item'''
        
        # Try to position the arm dynamically (according to tags location)
        try:
            await self.static_trex_position()
            await self.dynamic_trex_position()

        # Try to position the arm statically (according to const joints values)
        except Exception as e:
            self.log.debug("Couldn't perform dynamic trex positioning. \
                           Performing static trex positioning...")
            try:
                await self.static_trex_position()

            except Exception as e:
                await self.return_arm_home()
                if self.position_attempts > MAX_POSITION_ATTEMPTS:
                    self.log.warn(f'ERROR IN enter_POSITION_ARM - {e}')
                    self.abort(*ERROR_COULDNT_POSITION_ARM)

    

    async def enter_PICK_ITEM(self):
        pass


#--------------------------------- DEBUG ------------------------------------#
    async def enter_DEBUG_STATE(self):
         # Enable model
        self.log.info('Enabling apriltags model...')

        self.predictor_handler = await self.cv.enable_model(
                model = 'detector',type = 'tag',
                name = 'apriltags', 
                source = self.setup_args['working_camera'],
                model_params = {
                'families' : 'tag36h11',
                'nthreads' : 4,
                'quad_decimate' : 2.0,
                'quad_sigma': 0.0,
                'decode_sharpening' : 0.25,
                'refine_edges' : 1,
                'tag_size' : self.setup_args['tag_size']
                }
            )
        
         # Create listeners
        await self.predictor_handler.find_tags(
                tags = self.tags_info, 
                callback = self.callback_specific_tags
            )
        
        self.predictor_handler.set_img_detections_callback(
                callback = self.callback_predictions,
                as_dict = True,
                call_without_detections = True,
                cameras_controller = self.cameras
            )

        # Start timer
        self.detection_start_time = time.time()
#--------------------------------- DEBUG ------------------------------------#


    ###---------------------------- TRANSITIONS ----------------------------###
    
    async def transition_from_NAVIGATING_TO_CART(self):
        await self.check_navigation_success()
        if self.navigation_successful:
            self.navigation_successful = False
            self.set_state('APPROACHING_CART')

        else:
            self.navigation_counter += 1
            if self.navigation_counter >= MAX_NAVIGATION_ATTEMPTS:
                self.abort(*ERROR_COULDNT_REACH_DESTINATION)
            self.set_state('NAVIGATING_TO_CART')



    async def transition_from_APPROACHING_CART(self):
        await self.check_approach_success()
        if self.approach_successful:
            self.approach_successful = False
            current_position = await self.navigation.get_position(
                                                pos_unit = POSITION_UNIT.METERS,
                                                ang_unit = ANGLE_UNIT.DEGREES)
            self.approach_angle_error = self.execute_args['angle_to_goal'] - \
                                                            current_position[2] 
            self.set_state('DETECTING_TAGS_1')
        
        else:
            self.approach_counter += 1
            if self.approach_counter >= MAX_APPROACH_ATTEMPTS:
                self.abort(*ERROR_COULDNT_APPROACH_CART)
            self.set_state('APPROACHING_CART')

    

    async def transition_from_DETECTING_TAGS_1(self):
        await self.sleep(1.5)
        if self.tags_detected:
            self.tags_detected = False
            await self.motion.rotate(angle = self.approach_angle_error,
                                    angular_speed = 10,
                                    wait = True)
            
            self.sideways_distance = self.pixels2meters()
            await self.send_feedback(
                {'roation correction' : f'{self.approach_angle_error} degrees',
                'sideways distance' : f'{self.sideways_distance} meters'}
            )
            self.set_state('MOVING_SIDEWAYS')

        else:
            await self.motion.move_linear(distance = 0.07,
                                            x_velocity = -0.05,
                                            enable_obstacles = False,
                                            wait = False)

        if (time.time() - self.detection_start_time) > NO_TARGET_TIMEOUT:
            self.abort(*ERROR_TAG_NOT_FOUND)

    

    async def transition_from_MOVING_SIDEWAYS(self):
        if not self.motion.is_moving(): 
            self.set_state('DETECTING_TAGS_2')



    async def transition_from_DETECTING_TAGS_2(self):
        self.reset_detections()
        await self.sleep(1.5)
        if self.tags_detected:
            self.tags_detected = False
            self.set_state('POSITION_ARM')
        
        elif (time.time() - self.detection_start_time) > NO_TARGET_TIMEOUT:
            self.abort(*ERROR_TAG_NOT_FOUND)



    async def transition_from_POSITION_ARM(self):
        current_pose = await self.arms.get_current_pose(self.arm_name)
        current_position = np.array(current_pose['position'])
        if all(abs(current_position - self.trex_position) <= ARM_ERROR_THRESHOLD):
            self.set_state('PICK_ITEM')

        else:
            self.position_attempts += 1
            if self.position_attempts == MAX_POSITION_ATTEMPTS:
                self.abort(*ERROR_ARM_POSITION_NOT_ACCURATE)
            else:
                self.position_attempts = 0
                self.set_state('POSITION_ARM')



    async def transition_from_PICK_ITEM(self):
        await self.static_trex_position()
        await self.return_arm_home()
        self.set_state('END')



#--------------------------------- DEBUG ------------------------------------#
    async def transition_from_DEBUG_STATE(self):
        self.reset_detections()
        await self.sleep(1.5)
        if self.tags_detected:
            self.tags_detected = False
            self.set_state('POSITION_ARM')
        
        elif (time.time() - self.detection_start_time) > NO_TARGET_TIMEOUT:
            self.abort(*ERROR_TAG_NOT_FOUND)
#--------------------------------- DEBUG ------------------------------------#
