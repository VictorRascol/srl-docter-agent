#!/usr/bin/env python
# coding=utf-8

import grpc
import datetime
import sys
import logging
import socket
import os
import re
import ipaddress
import json
import signal
import traceback
import subprocess

import sdk_service_pb2
import sdk_service_pb2_grpc
import lldp_service_pb2
import config_service_pb2
import sdk_common_pb2

# To report state back
import telemetry_service_pb2
import telemetry_service_pb2_grpc

# See opt/rh/rh-python36/root/usr/lib/python3.6/site-packages/sdk_protos/bfd_service_pb2.py
import bfd_service_pb2

from logging.handlers import RotatingFileHandler

############################################################
## Agent will start with this name
############################################################
agent_name='auto_config_agent'

############################################################
## Open a GRPC channel to connect to sdk_mgr on the dut
## sdk_mgr will be listening on 50053
############################################################
#channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_sdk_service_manager:50053')
channel = grpc.insecure_channel('127.0.0.1:50053')
metadata = [('agent_name', agent_name)]
stub = sdk_service_pb2_grpc.SdkMgrServiceStub(channel)

def SendKeepAlive():
    request = sdk_service_pb2.KeepAliveRequest()
    result = stub.KeepAlive(request=request, metadata=metadata)
    print( f'Status of KeepAlive response :: {result.status}' )

############################################################
## Subscribe to required event
## This proc handles subscription of: Interface, LLDP,
##                      Route, Network Instance, Config
############################################################
def Subscribe(stream_id, option):
    op = sdk_service_pb2.NotificationRegisterRequest.AddSubscription
    if option == 'lldp':
        entry = lldp_service_pb2.LldpNeighborSubscriptionRequest()
        # TODO filter out 'mgmt0' interfaces
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, lldp_neighbor=entry)
    elif option == 'cfg':
        entry = config_service_pb2.ConfigSubscriptionRequest()
        entry.key.js_path = '.' + agent_name
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, config=entry)
    elif option == 'bfd':
        entry = bfd_service_pb2.BfdSessionSubscriptionRequest()
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, bfd_session=entry)
    subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    print('Status of subscription response for {}:: {}'.format(option, subscription_response.status))

############################################################
## Subscribe to all the events that Agent needs
############################################################
def Subscribe_Notifications(stream_id):
    '''
    Agent will receive notifications to what is subscribed here.
    '''
    if not stream_id:
        logging.info("Stream ID not sent.")
        return False

    # Subscribe to config changes, first
    Subscribe(stream_id, 'cfg')

    Subscribe(stream_id, 'bfd')  # Test

    ##Subscribe to LLDP Neighbor Notifications -> moved
    ## Subscribe(stream_id, 'lldp')

############################################################
## Function to populate state of agent config
## using telemetry -- add/update info from state
############################################################
def Add_Telemetry(js_path, js_data):
    telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(channel)
    telemetry_update_request = telemetry_service_pb2.TelemetryUpdateRequest()
    telemetry_info = telemetry_update_request.state.add()
    telemetry_info.key.js_path = js_path
    telemetry_info.data.json_content = js_data
    logging.info(f"Telemetry_Update_Request :: {telemetry_update_request}")
    telemetry_response = telemetry_stub.TelemetryAddOrUpdate(request=telemetry_update_request, metadata=metadata)
    return telemetry_response

############################################################
## Function to populate state fields of the agent
## It updates command: info from state auto-config-agent
############################################################
def Update_Peer_State(peer_ip, status, bfd_flaps, now):
    _ip_key = '.'.join([i.zfill(3) for i in peer_ip.split('.')]) # sortable
    js_path = '.' + agent_name + '.peer{.peer_ip=="' + _ip_key + '"}'
    data = {
      "bfd_status" : { "value" : status },
      "bfd_flaps_last_hour" : 1234,
      "last_bfd_flap_timestamp" : { "value" : now.strftime("%Y-%m-%d %H:%M:%S") }
    }
    response = Add_Telemetry( js_path=js_path, js_data=json.dumps(data) )
    logging.info(f"Telemetry_Update_Response :: {response}")
    return True

##################################################################
## Proc to process the config Notifications received by auto_config_agent
## At present processing config from js_path containing agent_name
##################################################################
def Handle_Notification(obj, state):
    if obj.HasField('config') and obj.config.key.js_path != ".commit.end":
        logging.info(f"GOT CONFIG :: {obj.config.key.js_path}")
        if agent_name in obj.config.key.js_path:
            logging.info(f"Got config for agent, now will handle it :: \n{obj.config}\
                            Operation :: {obj.config.op}\nData :: {obj.config.data.json}")
            if obj.config.op == 2:
                logging.info(f"Delete auto-config-agent cli scenario")
                # if file_name != None:
                #    Update_Result(file_name, action='delete')
                response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
                logging.info('Handle_Config: Unregister response:: {}'.format(response))
                state = State() # Reset state, works?
            else:
                json_acceptable_string = obj.config.data.json.replace("'", "\"")
                data = json.loads(json_acceptable_string)
                if 'role' in data:
                    state.role = data['role']
                    logging.info(f"Got role :: {state.role}")
                if 'peerlinks_prefix' in data:
                    state.peerlinks_prefix = data['peerlinks_prefix']['value']
                    state.peerlinks = list(ipaddress.ip_network(data['peerlinks_prefix']['value']).subnets(new_prefix=31))
                if 'loopbacks_prefix' in data:
                    state.loopbacks = list(ipaddress.ip_network(data['loopbacks_prefix']['value']).subnets(new_prefix=32))
                if 'base_as' in data:
                    state.base_as = int( data['base_as']['value'] )
                if 'max_spines' in data:
                    state.max_spines = int( data['max_spines']['value'] )
                if 'max_leaves' in data:
                    state.max_leaves = int( data['max_leaves']['value'] )
                if 'bfd_flaps_per_hour_threshold' in data:
                    state.bfd_flap_threshold = int( data['bfd_flaps_per_hour_threshold']['value'] )
                return not state.role is None

    elif obj.HasField('lldp_neighbor') and not state.role is None:
        # Update the config based on LLDP info, if needed
        logging.info(f"process LLDP notification : {obj}")
        my_port = obj.lldp_neighbor.key.interface_name  # ethernet-1/x
        to_port = obj.lldp_neighbor.data.port_id
        peer_sys_name = obj.lldp_neighbor.data.system_name

        if my_port != 'mgmt0' and to_port != 'mgmt0' and hasattr(state,'peerlinks'):
          my_port_id = re.split("/",re.split("-",my_port)[1])[1]
          m = re.match("^ethernet-(\d+)/(\d+)$", to_port)
          if m:
            to_port_id = m.groups()[1]
          else:
            to_port_id = my_port_id  # FRR Linux host or other element not sending port name

          # For spine-spine connections, build iBGP
          if (state.role == 'ROLE_spine') and 'spine' not in peer_sys_name:
            _r = 0
            link_index = state.max_spines * (int(to_port_id) - 1) + int(my_port_id) - 1
          else:
            _r = 1
            link_index = state.max_spines * (int(my_port_id) - 1) + int(to_port_id) - 1

          router_id_changed = False
          if m and not hasattr(state,"router_id"): # Only for valid to_port, if not set
            state.router_id = f"1.1.{ 0 if state.role == 'ROLE_spine' else 1 }.{to_port_id}"
            router_id_changed = True

          # Configure IP on interface and BGP for leaves
          link_name = f"link{link_index}"
          if not hasattr(state,link_name):
             _ip = str( list(state.peerlinks[link_index].hosts())[_r] )
             _peer = str( list(state.peerlinks[link_index].hosts())[1-_r] )
             script_update_interface(
                 'spine' if state.role == 'ROLE_spine' else 'leaf',
                 my_port,
                 _ip + '/31',
                 obj.lldp_neighbor.data.system_description if m else 'host',
                 _peer if _r==1 else '*',
                 state.base_as + (int(to_port_id) if state.role != 'ROLE_spine' else 0),
                 state.router_id if router_id_changed else "",
                 state.base_as if (state.role == 'ROLE_leaf') else state.base_as + 1,
                 state.base_as if (state.role == 'ROLE_leaf') else state.base_as + state.max_leaves,
                 state.peerlinks_prefix
             )
             setattr( state, link_name, _ip )
             Update_Peer_State( _peer, "red", 0, datetime.datetime.now() )
    elif obj.HasField('bfd_session'):
        logging.info(f"process BFD notification : {obj}")
        now = datetime.datetime.now()
        src_ip_addr = obj.bfd_session.key.src_ip_addr.addr
        dst_ip_addr = obj.bfd_session.key.dst_ip_addr.addr

        # Integer, 4=UP
        status = obj.bfd_session.data.status  # data.src_if_id, not always set
        src_ip_str = ipaddress.ip_address(src_ip_addr).__str__()
        dst_ip_str = ipaddress.ip_address(dst_ip_addr).__str__()
        logging.info(f"BFD : src={src_ip_str} dst={dst_ip_str} status={status}")

        if not state.HasField(dst_ip_str):
           state[dst_ip_str] = { "flaps" : {} }
        flaps = state[dst_ip_str].flaps
        flaps[now] = status
        flaps_last_hour = 0
        keep_flaps = {}
        one_hour_ago = now - datetime.timedelta(hours=1)
        for i in sorted(flaps.keys(), reverse=True):
           if ( i > one_hour_ago ):
               ++flaps_last_hour
               keep_flaps[i] = flaps[i]
           else:
               break
        logging.info(f"BFD : keeping last hour of flaps {keep_flaps}")
        state[dst_ip_str].flaps = keep_flaps
        Update_Peer_State( dst_ip_str,
            "red" if flaps_last_hour > state.bfd_flap_threshold else "green",
            flaps_last_hour, now )
    else:
        logging.info(f"Unexpected notification : {obj}")

    # dont subscribe to LLDP now
    return False
##################################################################################################
## This functions get the app_id from idb for a given app_name
##################################################################################################
def get_app_id(app_name):
    logging.info(f'Metadata {metadata} ')
    appId_req = sdk_service_pb2.AppIdRequest(name=app_name)
    app_id_response=stub.GetAppId(request=appId_req, metadata=metadata)
    logging.info(f'app_id_response {app_id_response.status} {app_id_response.id} ')
    return app_id_response.id

###########################
# JvB: Invokes gnmic client to update interface configuration, via bash script
###########################
def script_update_interface(role,name,ip,peer,peer_ip,_as,router_id,peer_as_min,peer_as_max,peer_links):
    logging.info(f'Calling update script: role={role} name={name} ip={ip} peer_ip={peer_ip} peer={peer} as={_as} ' +
                 f'router_id={router_id} peer_links={peer_links}' )
    try:
       script_proc = subprocess.Popen(['/etc/opt/srlinux/appmgr/gnmic-configure-interface.sh',
                                       role,name,ip,peer,peer_ip,str(_as),router_id,
                                       str(peer_as_min),str(peer_as_max),peer_links],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
       stdoutput, stderroutput = script_proc.communicate()
       logging.info(f'script_update_interface result: {stdoutput} err={stderroutput}')
    except Exception as e:
       logging.error(f'Exception caught in script_update_interface :: {e}')

class State(object):
    def __init__(self):
        self.role = None       # May not be set in config

    def __str__(self):
        return str(self.__class__) + ": " + str(self.__dict__)

##################################################################################################
## This is the main proc where all processing for auto_config_agent starts.
## Agent registration, notification registration, Subscrition to notifications.
## Waits on the subscribed Notifications and once any config is received, handles that config
## If there are critical errors, Unregisters the fib_agent gracefully.
##################################################################################################
def Run():
    sub_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(channel)

    # optional agent_liveliness=<seconds> to have system kill unresponsive agents
    response = stub.AgentRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
    logging.info(f"Registration response : {response.status}")

    app_id = get_app_id(agent_name)
    if not app_id:
        logging.error(f'idb does not have the appId for {agent_name} : {app_id}')
    else:
        logging.info(f'Got appId {app_id} for {agent_name}')

    request=sdk_service_pb2.NotificationRegisterRequest(op=sdk_service_pb2.NotificationRegisterRequest.Create)
    create_subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    stream_id = create_subscription_response.stream_id
    logging.info(f"Create subscription response received. stream_id : {stream_id}")

    Subscribe_Notifications(stream_id)

    stream_request = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
    stream_response = sub_stub.NotificationStream(stream_request, metadata=metadata)

    state = State()
    count = 1
    lldp_subscribed = False
    try:
        for r in stream_response:
            logging.info(f"Count :: {count}  NOTIFICATION:: \n{r.notification}")
            count += 1
            for obj in r.notification:
                if obj.HasField('config') and obj.config.key.js_path == ".commit.end":
                    logging.info('TO DO -commit.end config')
                else:
                    if Handle_Notification(obj, state) and not lldp_subscribed:
                       Subscribe(stream_id, 'lldp')
                       lldp_subscribed = True

                    # Program router_id only when changed
                    # if state.router_id != old_router_id:
                    #   gnmic(path='/network-instance[name=default]/protocols/bgp/router-id',value=state.router_id)
                    logging.info(f'Updated state: {state}')

    except grpc._channel._Rendezvous as err:
        logging.info(f'GOING TO EXIT NOW: {err}')

    except Exception as e:
        logging.error(f'Exception caught :: {e}')
        #if file_name != None:
        #    Update_Result(file_name, action='delete')
        try:
            response = stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
            logging.error(f'Run try: Unregister response:: {response}')
        except grpc._channel._Rendezvous as err:
            logging.info(f'GOING TO EXIT NOW: {err}')
            sys.exit()
        return True
    sys.exit()
    return True
############################################################
## Gracefully handle SIGTERM signal
## When called, will unregister Agent and gracefully exit
############################################################
def Exit_Gracefully(signum, frame):
    logging.info("Caught signal :: {}\n will unregister fib_agent".format(signum))
    try:
        response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
        logging.error('try: Unregister response:: {}'.format(response))
        sys.exit()
    except grpc._channel._Rendezvous as err:
        logging.info('GOING TO EXIT NOW: {}'.format(err))
        sys.exit()

##################################################################################################
## Main from where the Agent starts
## Log file is written to: /var/log/srlinux/stdout/<dutName>_fibagent.log
## Signals handled for graceful exit: SIGTERM
##################################################################################################
if __name__ == '__main__':
    # hostname = socket.gethostname()
    stdout_dir = '/var/log/srlinux/stdout' # PyTEnv.SRL_STDOUT_DIR
    signal.signal(signal.SIGTERM, Exit_Gracefully)
    if not os.path.exists(stdout_dir):
        os.makedirs(stdout_dir, exist_ok=True)
    log_filename = '{}/auto_config_agent.log'.format(stdout_dir)
    logging.basicConfig(filename=log_filename, filemode='a',\
                        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',\
                        datefmt='%H:%M:%S', level=logging.INFO)
    handler = RotatingFileHandler(log_filename, maxBytes=3000000,backupCount=5)
    logging.getLogger().addHandler(handler)
    logging.info("START TIME :: {}".format(datetime.datetime.now()))
    if Run():
        logging.info('Agent unregistered and agent routes withdrawed from dut')
    else:
        logging.info('Should not happen')
