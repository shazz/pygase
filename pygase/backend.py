# -*- coding: utf-8 -*-
"""Serve PyGaSe clients.

Provides the `Server` class and all PyGaSe components that deal with progression and syncing of game states.

# Contents
- #GameStateStore: main API class for game state repositories
- #Server: main API class for PyGaSe servers
- #GameStateMachine: main API class for game logic components
- #Backend: main API class for a fully integrated PyGaSe backend

"""

import time
import threading

import curio
from curio import socket
from curio.meta import awaitable

from pygase.connection import ServerConnection
from pygase.gamestate import GameState, GameStateUpdate, GameStatus
from pygase.event import UniversalEventHandler, Event
from pygase.utils import logger


class GameStateStore:

    """Provide access to a game state and manage state updates.

    # Arguments
    inital_game_state (GameState): state of the game before the simulation begins

    # Raises
    TypeError: if 'initial_game_state' is not an instance of #GameState

    """

    _update_cache_size: int = 100

    def __init__(self, initial_game_state: GameState = None):
        logger.debug("Creating GameStateStore instance.")
        self._game_state = initial_game_state if initial_game_state is not None else GameState()
        if not isinstance(self._game_state, GameState):
            raise TypeError(
                f"'initial_game_state' should be of type 'GameState', not '{self._game_state.__class__.__name__}'."
            )
        self._game_state_update_cache = [GameStateUpdate(0)]

    def get_update_cache(self) -> list:
        """Return the latest state updates."""
        return self._game_state_update_cache.copy()

    def get_game_state(self) -> GameState:
        """Return the current game state."""
        return self._game_state

    def push_update(self, update: GameStateUpdate) -> None:
        """Push a new state update to the update cache.

        This method will usually be called by whatever is progressing the game state,
        usually a #GameStateMachine.

        """
        self._game_state_update_cache.append(update)
        if len(self._game_state_update_cache) > self._update_cache_size:
            del self._game_state_update_cache[0]
        if update > self._game_state:
            logger.debug(
                (
                    f"Updating game state in state store from time order {self._game_state.time_order} "
                    f"to {update.time_order}."
                )
            )
            self._game_state += update


class Server:

    """Listen to clients and orchestrate the flow of events and state updates.

    The #Server instance does not contain game logic or state, it is only responsible for connections
    to clients. The state is provided by a #GameStateStore and game logic by a #GameStateMachine.

    # Arguments
    game_state_store (GameStateStore): part of the backend that provides an interface to the #pygase.GameState

    # Attributes
    connections (list): contains each clients address as a key leading to the
        corresponding #pygase.connection.ServerConnection instance
    host_client (tuple): address of the host client (who has permission to shutdown the server), if there is any
    game_state_store (GameStateStore): game state repository

    # Members
    hostname (str): read-only access to the servers hostname
    port (int): read-only access to the servers port number

    """

    def __init__(self, game_state_store: GameStateStore):
        logger.debug("Creating Server instance.")
        self.connections: dict = {}
        self.host_client: tuple = None
        self.game_state_store = game_state_store
        self._universal_event_handler = UniversalEventHandler()
        self._hostname: str = None
        self._port: int = None

    def run(self, port: int = 0, hostname: str = "localhost", event_wire=None) -> None:
        """Start the server under a specified address.

        This is a blocking function but can also be spawned as a coroutine or in a thread
        via #Server.run_in_thread().

        # Arguments
        port (int): port number the server will be bound to, default will be an available
           port chosen by the computers network controller
        hostname (str): hostname or IP address the server will be bound to.
           Defaults to `'localhost'`.
        event_wire (GameStateMachine): object to which events are to be repeated
           (has to implement a `_push_event(event)` method and is typically a #GameStateMachine)

        """
        curio.run(self.run, port, hostname, event_wire)

    @awaitable(run)
    async def run(  # pylint: disable=function-redefined
        self, port: int = 0, hostname: str = "localhost", event_wire=None
    ) -> None:
        # pylint: disable=missing-docstring
        await ServerConnection.loop(hostname, port, self, event_wire)

    def run_in_thread(
        self, port: int = 0, hostname: str = "localhost", event_wire=None, daemon=True
    ) -> threading.Thread:
        """Start the server in a seperate thread.

        See #Server.run().

        # Returns
        threading.Thread: the thread the server loop runs in

        """
        thread = threading.Thread(target=self.run, args=(port, hostname, event_wire), daemon=daemon)
        thread.start()
        return thread

    @property
    def hostname(self) -> str:
        """Get the hostname or IP address on which the server listens.

        Returns `None` when the server is not running.

        """
        return "localhost" if self._hostname == "127.0.0.1" else self._hostname

    @property
    def port(self) -> int:
        """Get the port number on which the server listens.

        Returns `None` when the server is not running.

        """
        return self._port

    def shutdown(self) -> None:
        """Shut down the server.

        The server can be restarted via #Server.run() in which case it will remember previous connections.
        This method can also be spawned as a coroutine.

        """
        curio.run(self.shutdown)

    @awaitable(shutdown)
    async def shutdown(self) -> None:  # pylint: disable=function-redefined
        # pylint: disable=missing-docstring
        async with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            await sock.sendto("shut_me_down".encode("utf-8"), (self._hostname, self._port))

    # advanced type checking for target client and callback would be helpful
    def dispatch_event(
        self, event_type: str, *args, target_client="all", retries: int = 0, ack_callback=None, **kwargs
    ) -> None:
        """Send an event to one or all clients.

        # Arguments
        event_type (str): identifies the event and links it to a handler
        target_client (tuple, str): either `'all'` for an event broadcast, or a clients address as a tuple
        retries (int): number of times the event is to be resent in case it times out
        ack_callback (callable, coroutine): will be executed after the event was received
            and be passed a reference to the corresponding #pygase.connection.ServerConnection instance

        Additional positional and keyword arguments will be sent as event data and passed to the clients
        handler function.

        """
        event = Event(event_type, *args, **kwargs)

        def get_ack_callback(connection):
            if ack_callback is not None:
                return lambda: ack_callback(connection)
            return None

        timeout_callback = None
        if retries > 0:

            timeout_callback = lambda: self.dispatch_event(  # type: ignore
                event_type,
                *args,
                target_client=target_client,
                retries=retries - 1,
                ack_callback=ack_callback,
                **kwargs,
            ) or logger.warning(  # type: ignore
                f"Event of type {event_type} timed out. Retrying to send event to server."
            )

        if target_client == "all":
            for connection in self.connections.values():
                connection.dispatch_event(event, get_ack_callback(connection), timeout_callback, **kwargs)
        else:
            self.connections[target_client].dispatch_event(
                event, get_ack_callback(self.connections[target_client]), timeout_callback, **kwargs
            )

    # add advanced type checking for handler functions
    def register_event_handler(self, event_type: str, event_handler_function) -> None:
        """Register an event handler for a specific event type.

        # Arguments
        event_type (str): event type to link the handler function to
        handler_func (callable, coroutine): will be called for received events of the given type

        """
        self._universal_event_handler.register_event_handler(event_type, event_handler_function)


class GameStateMachine:

    """Run a simulation that propagates the game state.

    A #GameStateMachine progresses a game state through time, applying all game simulation logic.
    This class is meant either as a base class from which you inherit and implement the #GameStateMachine.time_step()
    method, or you assign an implementation after instantiation.

    # Arguments
    game_state_store (GameStateStore): part of the PyGaSe backend that provides the state

    # Attributes
    game_time (float): duration the game has been running in seconds

    """

    def __init__(self, game_state_store: GameStateStore):
        logger.debug("Creating GameStateMachine instance.")
        self.game_time: float = 0.0
        self._event_queue = curio.UniversalQueue()
        self._universal_event_handler = UniversalEventHandler()
        self._game_state_store = game_state_store
        self._game_loop_is_running = False

    def _push_event(self, event: Event) -> None:
        """Push an event into the state machines event queue.

        This method can be spawned as a coroutine.

        """
        logger.debug(f"State machine receiving event of type {event.type} via event wire.")
        self._event_queue.put(event)

    @awaitable(_push_event)
    async def _push_event(self, event: Event) -> None:  # pylint: disable=function-redefined
        logger.debug(f"State machine receiving event of type {event.type} via event wire.")
        await self._event_queue.put(event)

    # advanced type checking for the handler function would be helpful
    def register_event_handler(self, event_type: str, event_handler_function) -> None:
        """Register an event handler for a specific event type.

        For event handlers to have any effect, the events have to be wired from a #Server to
        the #GameStateMachine via the `event_wire` argument of the #Server.run() method.

        # Arguments
        event_type (str): which type of event to link the handler function to
        handler_func (callable, coroutine): function or coroutine to be invoked for events of the given type

        ---
        In addition to the event data, a #GameStateMachine handler function gets passed
        the following keyword arguments

        - `game_state`: game state at the time of the event
        - `dt`: time since the last time step
        - `client_address`: client which sent the event that is being handled

        It is expected to return an update dict like the `time_step` method.

        """
        self._universal_event_handler.register_event_handler(event_type, event_handler_function)

    def run_game_loop(self, interval: float = 0.02) -> None:
        """Simulate the game world.

        This function blocks as it continously progresses the game state through time
        but it can also be spawned as a coroutine or in a thread via #Server.run_game_loop_in_thread().
        As long as the simulation is running, the `game_state.status` will be `GameStatus.get('Active')`.

        # Arguments
        interval (float): (minimum) duration in seconds between consecutive time steps

        """
        curio.run(self.run_game_loop, interval)

    @awaitable(run_game_loop)
    async def run_game_loop(self, interval: float = 0.02) -> None:  # pylint: disable=function-redefined
        # pylint: disable=missing-docstring
        if self._game_state_store.get_game_state().game_status == GameStatus.get("Paused"):
            self._game_state_store.push_update(
                GameStateUpdate(
                    self._game_state_store.get_game_state().time_order + 1, game_status=GameStatus.get("Active")
                )
            )
        game_state = self._game_state_store.get_game_state()
        dt = interval
        self._game_loop_is_running = True
        logger.info(f"State machine starting game loop with interval of {interval} seconds.")
        while game_state.game_status == GameStatus.get("Active"):
            t0 = time.perf_counter()

            update_dict = self.time_step(game_state, dt)
            while not self._event_queue.empty():
                event = await self._event_queue.get()
                event_update = await self._universal_event_handler.handle(event, game_state=game_state, dt=dt)
                update_dict.update(event_update)
                if time.perf_counter() - t0 > 0.95 * interval:
                    break

            self._game_state_store.push_update(GameStateUpdate(game_state.time_order + 1, **update_dict))
            game_state = self._game_state_store.get_game_state()

            # real elapsed time since the loop start
            real_elapsed_time = time.perf_counter() - t0

            # consume the remaining time to reach the interval
            delta = interval - real_elapsed_time
            while delta > 0.0000001:
                delta = interval - (time.perf_counter() - t0)

            # adding the real spent time to the game time
            dt = interval - delta
            self.game_time += dt
            logger.info(f"loop time: {round(dt, 4)} vs expected {round(interval, 4)} => delta {round(dt - interval, 6)}")

        logger.info("Game loop stopped.")
        self._game_loop_is_running = False

    def run_game_loop_in_thread(self, interval: float = 0.02) -> threading.Thread:
        """Simulate the game in a seperate thread.

        See #GameStateMachine.run_game_loop().

        # Returns
        threading.Thread: the thread the game loop runs in

        """
        thread = threading.Thread(target=self.run_game_loop, args=(interval,))
        thread.start()
        return thread

    def stop(self, timeout: float = 1.0) -> bool:
        """Pause the game simulation.

        This sets `self.status` to `Gamestatus.get('Paused')`. This method can also be spawned as a coroutine.
        A subsequent call of #GameStateMachine.run_game_loop() will resume the simulation at the point
        where it was stopped.

        # Arguments
        timeout (float): time in seconds to wait for the simulation to stop

        # Returns
        bool: wether or not the simulation was successfully stopped

        """
        return curio.run(self.stop, timeout)

    @awaitable(stop)
    async def stop(self, timeout: float = 1.0) -> bool:  # pylint: disable=function-redefined
        # pylint: disable=missing-docstring
        logger.info("Trying to stop game loop ...")
        if self._game_state_store.get_game_state().game_status == GameStatus.get("Active"):
            self._game_state_store.push_update(
                GameStateUpdate(
                    self._game_state_store.get_game_state().time_order + 1, game_status=GameStatus.get("Paused")
                )
            )
        t0 = time.time()
        while self._game_loop_is_running:
            if time.time() - t0 > timeout:
                break
            await curio.sleep(0)
        return not self._game_loop_is_running

    def time_step(self, game_state: GameState, dt: float) -> dict:
        """Calculate a game state update.

        This method should be implemented to return a dict with all the updated state attributes.

        # Arguments
        game_state (GameState): the state of the game prior to the time step
        dt (float): time in seconds since the last time step, use it to simulate at a consistent speed

        # Returns
        dict: updated game state attributes

        """
        raise NotImplementedError()


class Backend:

    """Easily create a fully integrated PyGaSe backend.

    # Arguments
    initial_game_state (GameState): state of the game before the simulation begins
    time_step_function (callable): function that takes a game state and a time difference and returns
        a dict of updated game state attributes (see #GameStateMachine.time_step())
    event_handlers (dict): a dict with event types as keys and event handler functions as values

    # Attributes
    game_state_store (GameStateStore): the backends game state repository
    game_state_machine (GameStateMachine): logic component that runs the game loop
    server (Server): handles connections to PyGaSe clients

    # Example
    ```python
    # Run a game loop that continuously increments `foo` with velocity `bar`.
    Backend(
        initial_gamestate=GameState(foo=0.0, bar=0.5),
        time_step_function=lambda game_state, dt: {foo: game_state.foo + game_state.bar*dt},
        # Handle client events to reset `foo` and set a new `bar` value.
        event_handlers={
            "RESET_FOO": lambda game_state, dt: {foo: 0.0},
            "SET_BAR": lambda new_bar, game_state, dt: {bar: new_bar}
        }
    ).run(hostname="localhost", port=8080)
    ```

    """

    def __init__(self, initial_game_state: GameState, time_step_function, event_handlers: dict = None):
        logger.info("Assembling Backend ...")
        self.game_state_store = GameStateStore(initial_game_state)
        self.game_state_machine = GameStateMachine(self.game_state_store)
        setattr(self.game_state_machine, "time_step", time_step_function)
        if event_handlers is not None:
            for event_type, handler_function in event_handlers.items():
                self.game_state_machine.register_event_handler(event_type, handler_function)
        self.server = Server(self.game_state_store)
        logger.info("Backend assembled and ready.")

    def run(self, hostname: str, port: int, interval: float = 0.02):
        """Run state machine and server and bind the server to a given address.

        # Arguments
        hostname (str): hostname or IPv4 address the server will be bound to
        port (int): port number the server will be bound to
        interval (float): (minimum) duration in seconds between consecutive time steps

        """
        self.game_state_machine.run_game_loop_in_thread(interval=interval)
        self.server.run(port, hostname, self.game_state_machine)
        self.game_state_machine.stop()
        logger.info("Backend successfully shut down.")

    def shutdown(self):
        """Shut down server and stop game loop."""
        self.server.shutdown()
