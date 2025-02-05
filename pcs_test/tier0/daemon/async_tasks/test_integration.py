import dataclasses
import signal
from datetime import timedelta
from logging import Logger
from multiprocessing import Process
from multiprocessing.pool import worker as mp_worker_init  # type: ignore
from queue import Queue
from unittest import mock

from tornado.testing import gen_test

from pcs import settings
from pcs.common.async_tasks.dto import (
    CommandDto,
    CommandOptionsDto,
)
from pcs.common.async_tasks.types import (
    TaskFinishType,
    TaskKillReason,
)
from pcs.daemon.async_tasks.scheduler import TaskNotFoundError
from pcs.daemon.async_tasks.types import Command
from pcs.daemon.async_tasks.worker import executor
from pcs.daemon.async_tasks.worker.report_processor import WorkerReportProcessor
from pcs.daemon.async_tasks.worker.types import (
    Message,
    TaskExecuted,
    TaskFinished,
)

from .dummy_commands import (
    RESULT,
    test_command_map,
)
from .helpers import (
    AUTH_USER,
    DATETIME_NOW,
    AssertTaskStatesMixin,
    MockDateTimeNowMixin,
    MockOsKillMixin,
    PermissionsCheckerMock,
    SchedulerBaseAsyncTestCase,
)

COMMAND_OPTIONS = CommandOptionsDto(request_timeout=None)
executor.worker_com = Queue()  # patched at runtime


class IntegrationBaseTestCase(SchedulerBaseAsyncTestCase):
    async def perform_actions(self, message_count):
        # pylint: disable=protected-access
        """
        USE THIS FUNCTION IN TIER0 TESTS instead of the scheduler function with
        the same name to guarantee consistency between test runs. This function
        guarantees that worker_com is emptied in one call, removing variability
        between test runs.
        """
        # TODO: remove this function
        del message_count
        await self.scheduler.perform_actions()

    def execute_tasks(self, task_ident_list):
        """Simulates process pool workers launching tasks

        Emits one message into worker_com queue per task. Process IDs of these
        tasks are task_idents stripped of the "id" prefix.
        :param task_ident_list: Contains task_idents of tasks to execute
        """
        for task_ident in task_ident_list:
            self.worker_com.put_nowait(
                Message(task_ident, TaskExecuted(int(task_ident[2:])))
            )

    def finish_tasks(
        self, task_ident_list, finish_type=TaskFinishType.SUCCESS, result=None
    ):
        """Simulates process pool workers handing over results when task ends

        Emits one message into worker_com queue per task
        :param task_ident_list: Contains task_idents of tasks that finished
        :param finish_type: Task finish type for all task_idents
        :param result: Return value of an executed function for all task_idents
        """
        for task_ident in task_ident_list:
            self.worker_com.put_nowait(
                Message(task_ident, TaskFinished(finish_type, result))
            )


class StateChangeTest(AssertTaskStatesMixin, IntegrationBaseTestCase):
    """Tests that check that task state is correct at each stage

    Task state changes prompted by errors or other special events are tested
    in the specific classes, these are just baseline tests for how everything
    should work in an ideal (error-free) scenario.
    """

    @gen_test
    async def test_created_from_empty(self):
        self._create_tasks(5)
        self.assert_task_state_counts_equal(5, 0, 0, 0)

    @gen_test
    async def test_created_on_top_of_existing(self):
        self._create_tasks(5)
        await self.perform_actions(0)
        self.execute_tasks(["id0", "id1", "id2"])
        await self.perform_actions(3)
        self._create_tasks(2, start_from=5)
        # 3/5 were executed, remaining are queued, 2 new arrived
        self.assert_task_state_counts_equal(2, 2, 3, 0)

    @gen_test
    async def test_created_to_scheduled(self):
        self._create_tasks(4)
        await self.perform_actions(0)
        self.assert_task_state_counts_equal(0, 4, 0, 0)

    @gen_test
    async def test_scheduled_to_executed(self):
        self._create_tasks(4)
        await self.perform_actions(0)
        # Tasks are scheduled, now 2 will start executing
        self.execute_tasks(["id0", "id1"])
        await self.perform_actions(2)
        self.assert_task_state_counts_equal(0, 2, 2, 0)

    @gen_test
    async def test_executed_to_finished(self):
        self._create_tasks(1)
        await self.perform_actions(0)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        self.finish_tasks(["id0"])
        await self.perform_actions(1)
        self.assert_task_state_counts_equal(0, 0, 0, 1)


class GarbageCollectionTimeoutTests(
    MockOsKillMixin, MockDateTimeNowMixin, IntegrationBaseTestCase
):
    """Testing garbage collection after timeouts

    Testing garbage collection for events caused by the scheduler like timeouts.
    """

    def setUp(self):
        super().setUp()
        # This mock already sets current time to DATETIME_NOW
        self.mock_datetime_now = self._init_mock_datetime_now()
        self.mock_os_kill = self._init_mock_os_kill()

    async def _run_gc_and_assert_state(
        self, timeout_s, task_finish_type, task_kill_reason
    ):
        self.mock_datetime_now.return_value = DATETIME_NOW + timedelta(
            seconds=timeout_s + 1
        )
        await self.perform_actions(0)
        task_info = self.scheduler.get_task("id0", AUTH_USER)
        self.assertEqual(task_finish_type, task_info.task_finish_type)
        self.assertEqual(task_kill_reason, task_info.kill_reason)

    @gen_test
    async def test_get_task_removes_finished(self):
        self._create_tasks(1)
        await self.perform_actions(0)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        self.finish_tasks(["id0"])
        await self.perform_actions(1)
        self.scheduler.get_task("id0", AUTH_USER)
        # pylint: disable=protected-access
        self.assertIsNotNone(
            self.scheduler._task_register["id0"]._to_delete_timestamp
        )

    @gen_test
    async def test_created_defunct_timeout(self):
        # Nothing should happen, created tasks can't be defunct
        self._create_tasks(1)
        await self._run_gc_and_assert_state(
            settings.task_unresponsive_timeout_seconds,
            TaskFinishType.UNFINISHED,
            task_kill_reason=None,
        )

    @gen_test
    async def test_scheduled_defunct_timeout(self):
        # Nothing should happen, scheduled tasks can't be defunct
        self._create_tasks(1)
        await self.perform_actions(0)
        await self._run_gc_and_assert_state(
            settings.task_unresponsive_timeout_seconds,
            TaskFinishType.UNFINISHED,
            task_kill_reason=None,
        )

    @gen_test
    async def test_executed_defunct_timeout(self):
        # Task should be killed
        self._create_tasks(1)
        await self.perform_actions(0)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        await self._run_gc_and_assert_state(
            settings.task_unresponsive_timeout_seconds,
            TaskFinishType.KILL,
            TaskKillReason.COMPLETION_TIMEOUT,
        )

    @gen_test
    async def test_finished_defunct_timeout(self):
        # Only tasks in EXECUTED state can become defunct. In this case,
        # task is going to become ABANDONED and is deleted
        self._create_tasks(2)
        await self.perform_actions(0)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        await self._run_gc_and_assert_state(
            settings.task_unresponsive_timeout_seconds,
            TaskFinishType.KILL,
            TaskKillReason.COMPLETION_TIMEOUT,
        )
        await self.perform_actions(0)
        with self.assertRaises(TaskNotFoundError):
            self.scheduler.get_task("id0", AUTH_USER)
        # If the guard task was removed, this fails the test case
        self.scheduler.get_task("id1", AUTH_USER)

    @gen_test
    async def test_created_abandoned_timeout(self):
        self._create_tasks(1)
        await self._run_gc_and_assert_state(
            settings.task_abandoned_timeout_seconds,
            TaskFinishType.UNFINISHED,
            task_kill_reason=None,
        )

    @gen_test
    async def test_scheduled_abandoned_timeout(self):
        self._create_tasks(1)
        await self.perform_actions(0)
        await self._run_gc_and_assert_state(
            settings.task_abandoned_timeout_seconds,
            TaskFinishType.UNFINISHED,
            task_kill_reason=None,
        )

    @gen_test
    async def test_executed_abandoned_timeout(self):
        self._create_tasks(1)
        await self.perform_actions(0)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        await self._run_gc_and_assert_state(
            settings.task_abandoned_timeout_seconds,
            TaskFinishType.UNFINISHED,
            task_kill_reason=None,
        )

    @gen_test
    async def test_finished_abandoned_timeout(self):
        self._create_tasks(1)
        await self.perform_actions(0)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        self.finish_tasks(["id0"])
        await self.perform_actions(1)
        self.mock_datetime_now.return_value = DATETIME_NOW + timedelta(
            seconds=settings.task_abandoned_timeout_seconds + 1
        )
        # Garbage collector deletes an abandoned task right away
        await self.perform_actions(0)
        with self.assertRaises(TaskNotFoundError):
            self.scheduler.get_task("id0", AUTH_USER)


class GarbageCollectionUserKillTests(
    MockOsKillMixin, AssertTaskStatesMixin, IntegrationBaseTestCase
):
    """Testing garbage collection after user kills a task

    Tests the garbage collection after user intervention to running tasks.
    """

    def setUp(self):
        super().setUp()
        self.mock_os_kill = self._init_mock_os_kill()

    def assert_end_state(self):
        task_info_killed = self.scheduler.get_task("id0", AUTH_USER)
        self.assertEqual(TaskFinishType.KILL, task_info_killed.task_finish_type)
        self.assertEqual(TaskKillReason.USER, task_info_killed.kill_reason)

        task_info_alive = self.scheduler.get_task("id1", AUTH_USER)
        self.assertEqual(
            TaskFinishType.UNFINISHED, task_info_alive.task_finish_type
        )
        self.assertIsNone(task_info_alive.kill_reason)

    @gen_test
    async def test_kill_created(self):
        self._create_tasks(2)
        self.scheduler.kill_task("id0", AUTH_USER)
        # Kill_task doesn't produce any messages since the worker is killed by
        # the system
        await self.perform_actions(0)
        self.assert_task_state_counts_equal(0, 1, 0, 1)

        self.mock_os_kill.assert_not_called()
        self.assert_end_state()

    @gen_test
    async def test_kill_scheduled(self):
        self._create_tasks(2)
        await self.perform_actions(0)
        self.scheduler.kill_task("id0", AUTH_USER)
        # Garbage collection waits until the task is executed and then kills
        # the worker
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        await self.perform_actions(0)
        self.assert_task_state_counts_equal(0, 1, 0, 1)

        self.mock_os_kill.assert_called_once()
        self.assert_end_state()

    @gen_test
    async def test_kill_executed(self):
        self._create_tasks(2)
        await self.perform_actions(0)
        self.execute_tasks(["id0", "id1"])
        await self.perform_actions(2)
        self.scheduler.kill_task("id0", AUTH_USER)
        await self.perform_actions(0)
        self.assert_task_state_counts_equal(0, 0, 1, 1)

        self.mock_os_kill.assert_called_once()
        self.assert_end_state()

    @gen_test
    async def test_kill_finished(self):
        self._create_tasks(2)
        await self.perform_actions(0)
        self.execute_tasks(["id0", "id1"])
        await self.perform_actions(2)
        self.finish_tasks(["id0"])
        await self.perform_actions(1)
        # When scheduler picks up finished tasks, it sends a signal to worker
        # to resume via os.kill
        self.mock_os_kill.reset_mock()
        self.scheduler.kill_task("id0", AUTH_USER)
        await self.perform_actions(0)
        self.assert_task_state_counts_equal(0, 0, 1, 1)

        self.mock_os_kill.assert_not_called()

        task_info_not_killed = self.scheduler.get_task("id0", AUTH_USER)
        self.assertEqual(
            TaskFinishType.SUCCESS, task_info_not_killed.task_finish_type
        )
        self.assertEqual(TaskKillReason.USER, task_info_not_killed.kill_reason)

        task_info_alive = self.scheduler.get_task("id1", AUTH_USER)
        self.assertEqual(
            TaskFinishType.UNFINISHED, task_info_alive.task_finish_type
        )
        self.assertIsNone(task_info_alive.kill_reason)


class TaskResultsTests(MockOsKillMixin, IntegrationBaseTestCase):
    """These tests check all task outcomes with real task_executor

    These test go one level deeper to include task_executor and test its
    behavior. All possible task outcomes are tested.
    """

    # pylint: disable=protected-access

    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch(
            "pcs.daemon.async_tasks.worker.executor.COMMAND_MAP",
            test_command_map,
        ).start()
        mock.patch(
            "pcs.daemon.async_tasks.worker.executor.getLogger", spec=Logger
        ).start()
        mock.patch(
            "pcs.daemon.async_tasks.worker.executor.worker_com",
            self.worker_com,
        ).start()
        lib_env_mock = (
            mock.patch(
                "pcs.daemon.async_tasks.worker.executor.LibraryEnvironment"
            )
            .start()
            .return_value
        )
        mock.patch(
            "pcs.daemon.async_tasks.worker.executor.PermissionsChecker",
            lambda _: PermissionsCheckerMock({}),
        ).start()
        lib_env_mock.report_processor = WorkerReportProcessor(
            self.worker_com, "id0"
        )
        # Os.kill is used to pause the worker and we do not want to pause tests
        self._init_mock_os_kill()

    def _new_task(self, task_id, cmd):
        with mock.patch(
            "pcs.daemon.async_tasks.scheduler.get_unique_uuid"
        ) as mock_uuid:
            mock_uuid.return_value = task_id
            self.scheduler.new_task(
                Command(CommandDto(cmd, {}, COMMAND_OPTIONS)),
                AUTH_USER,
            )

    @gen_test
    async def test_task_successful_no_result_with_reports(self):
        # How is no result different from a None return value in DTO?
        # Functions without return values also return None - should we
        # distinguish between cases of ex/implicitly returned None
        task_id = "id0"
        self._new_task(task_id, "success_with_reports")
        await self.perform_actions(0)
        # This task sends one report and returns immediately, task_executor
        # sends two messages - TaskExecuted and TaskFinished
        executor.task_executor(
            self.scheduler._task_register[task_id].to_worker_command()
        )
        await self.perform_actions(3)

        task_info = self.scheduler.get_task(task_id, AUTH_USER)
        self.assertEqual(1, len(task_info.reports))
        self.assertEqual(TaskFinishType.SUCCESS, task_info.task_finish_type)
        self.assertIsNone(task_info.result)

    @gen_test
    async def test_task_successful_with_result(self):
        task_id = "id0"
        self._new_task(task_id, "success")
        await self.perform_actions(0)
        # This task sends no reports and returns immediately, task_executor
        # sends two messages - TaskExecuted and TaskFinished
        executor.task_executor(
            self.scheduler._task_register[task_id].to_worker_command()
        )
        await self.perform_actions(2)

        task_info = self.scheduler.get_task(task_id, AUTH_USER)
        self.assertEqual(0, len(task_info.reports))
        self.assertEqual(TaskFinishType.SUCCESS, task_info.task_finish_type)
        self.assertEqual(RESULT, task_info.result)

    @gen_test
    async def test_task_error(self):
        task_id = "id0"
        self._new_task(task_id, "lib_exc")
        await self.perform_actions(0)
        # This task immediately raises a LibraryException and executor detects
        # that as an error, sends two messages - TaskExecuted and TaskFinished
        executor.task_executor(
            self.scheduler._task_register[task_id].to_worker_command()
        )
        await self.perform_actions(2)

        task_info = self.scheduler.get_task(task_id, AUTH_USER)
        self.assertEqual(0, len(task_info.reports))
        self.assertEqual(TaskFinishType.FAIL, task_info.task_finish_type)
        self.assertIsNone(task_info.result)

    @gen_test
    async def test_task_unhandled_exception(self):
        task_id = "id0"
        self._new_task(task_id, "unhandled_exc")
        await self.perform_actions(0)
        # This task immediately raises an Exception which the executor catches
        # and logs accordingly
        executor.task_executor(
            self.scheduler._task_register[task_id].to_worker_command()
        )
        await self.perform_actions(2)

        task_info = self.scheduler.get_task(task_id, AUTH_USER)
        self.assertEqual(0, len(task_info.reports))
        self.assertEqual(
            TaskFinishType.UNHANDLED_EXCEPTION, task_info.task_finish_type
        )
        self.assertIsNone(task_info.result)

    @gen_test
    async def test_wait_for_task(self):
        task_id = "id0"
        self._new_task(task_id, "success")
        await self.perform_actions(0)
        executor.task_executor(
            self.scheduler._task_register[task_id].to_worker_command()
        )
        await self.perform_actions(2)

        task_info = await self.scheduler.wait_for_task(task_id, AUTH_USER)
        self.assertEqual(0, len(task_info.reports))
        self.assertEqual(TaskFinishType.SUCCESS, task_info.task_finish_type)
        self.assertEqual(RESULT, task_info.result)


@mock.patch("pcs.daemon.async_tasks.task.os.kill")
class DeadlockTests(
    MockOsKillMixin, AssertTaskStatesMixin, IntegrationBaseTestCase
):
    # pylint: disable=protected-access

    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        self.scheduler._config = dataclasses.replace(
            self.scheduler._config, deadlock_threshold_timeout=0
        )
        self.process_cls_mock = mock.Mock()
        self.process_obj_mock = mock.Mock(spec=Process)
        self.process_cls_mock.return_value = self.process_obj_mock
        mock.patch(
            "pcs.daemon.async_tasks.scheduler.mp.Process", self.process_cls_mock
        ).start()

    @gen_test
    async def test_deadlock_mitigation(self, mock_kill):
        self._create_tasks(2)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        # deadlock detected, new tmp worker spawned
        self.assert_task_state_counts_equal(0, 1, 1, 0)
        self.process_cls_mock.assert_called_once_with(
            group=None,
            target=mp_worker_init,
            args=(
                self.mp_pool_mock._inqueue,
                self.mp_pool_mock._outqueue,
                executor.worker_init,
                (self.worker_com, self.logging_queue),
                1,
                False,
            ),
        )
        self.process_obj_mock.start.assert_called_once_with()
        self.process_obj_mock.close.assert_not_called()
        self.execute_tasks(["id1"])
        self.process_obj_mock.is_alive.return_value = True
        await self.perform_actions(0)
        # tmp worker started executing a task
        self.assert_task_state_counts_equal(0, 0, 2, 0)
        self.process_obj_mock.close.assert_not_called()
        self.finish_tasks(["id1"])
        self.process_obj_mock.is_alive.return_value = False
        mock_kill.assert_not_called()
        await self.perform_actions(1)
        mock_kill.assert_called_once_with(1, signal.SIGCONT)
        # tmp worker finished the task and terminated itself
        self.assert_task_state_counts_equal(0, 0, 1, 1)
        self.process_obj_mock.close.assert_called_once_with()

    @gen_test
    async def test_max_worker_count_reached(self, mock_kill):
        self.scheduler._config = dataclasses.replace(
            self.scheduler._config, max_worker_count=1
        )
        self._create_tasks(3)
        self.execute_tasks(["id0"])
        await self.perform_actions(1)
        self.assert_task_state_counts_equal(0, 2, 1, 0)
        self.process_cls_mock.assert_not_called()
        self.process_obj_mock.assert_not_called()
        mock_kill.assert_not_called()
