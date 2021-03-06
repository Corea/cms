#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2010-2013 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2015 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2012 Matteo Boscariol <boscarim@hotmail.com>
# Copyright © 2012-2014 Luca Wehrstedt <luca.wehrstedt@gmail.com>
# Copyright © 2014 Artem Iglikov <artem.iglikov@gmail.com>
# Copyright © 2014 Fabian Gundlach <320pointsguy@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Dataset-related handlers for AWS.

"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import json
import logging
import re
import zipfile

from StringIO import StringIO
import tornado.web

from cms.db import Dataset, Manager, Message, Participation, \
    Session, Submission, Task, Testcase
from cms.grading import compute_changes_for_dataset
from cmscommon.datetime import make_datetime

from .base import BaseHandler, require_permission


logger = logging.getLogger(__name__)


class DatasetSubmissionsHandler(BaseHandler):
    """Shows all submissions for this dataset, allowing the admin to
    view the results under different datasets.

    """
    @require_permission(BaseHandler.AUTHENTICATED)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        submission_query = self.sql_session.query(Submission)\
            .filter(Submission.task == task)
        page = int(self.get_query_argument("page", 0))
        self.render_params_for_submissions(submission_query, page)

        self.r_params["task"] = task
        self.r_params["active_dataset"] = task.active_dataset
        self.r_params["shown_dataset"] = dataset
        self.r_params["datasets"] = \
            self.sql_session.query(Dataset)\
                            .filter(Dataset.task == task)\
                            .order_by(Dataset.description).all()
        self.render("dataset.html", **self.r_params)


class CloneDatasetHandler(BaseHandler):
    """Clone a dataset by duplicating it (on the same task).

    It's equivalent to the old behavior of AddDatasetHandler when the
    dataset_id_to_copy given was the ID of an existing dataset.

    If referred by GET, this handler will return a HTML form.
    If referred by POST, this handler will create the dataset.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id_to_copy):
        dataset = self.safe_get_item(Dataset, dataset_id_to_copy)
        task = self.safe_get_item(Task, dataset.task_id)

        try:
            original_dataset = \
                self.safe_get_item(Dataset, dataset_id_to_copy)
            description = "Copy of %s" % original_dataset.description
        except ValueError:
            raise tornado.web.HTTPError(404)

        self.r_params = self.render_params()
        self.r_params["task"] = task
        self.r_params["clone_id"] = dataset_id_to_copy
        self.r_params["original_dataset"] = original_dataset
        self.r_params["original_dataset_task_type_parameters"] = \
            json.loads(original_dataset.task_type_parameters)
        self.r_params["default_description"] = description
        self.render("add_dataset.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, dataset_id_to_copy):
        fallback_page = "/dataset/%s/clone" % dataset_id_to_copy

        dataset = self.safe_get_item(Dataset, dataset_id_to_copy)
        task = self.safe_get_item(Task, dataset.task_id)
        task_id = task.id

        try:
            original_dataset = \
                self.safe_get_item(Dataset, dataset_id_to_copy)
        except ValueError:
            raise tornado.web.HTTPError(404)

        try:
            attrs = dict()

            self.get_string(attrs, "description")

            # Ensure description is unique.
            if any(attrs["description"] == d.description
                   for d in task.datasets):
                self.application.service.add_notification(
                    make_datetime(),
                    "Dataset name %r is already taken." % attrs["description"],
                    "Please choose a unique name for this dataset.")
                self.redirect(fallback_page)
                return

            self.get_time_limit(attrs, "time_limit")
            self.get_memory_limit(attrs, "memory_limit")
            self.get_task_type(attrs, "task_type", "TaskTypeOptions_")
            self.get_score_type(attrs, "score_type", "score_type_parameters")

            # Create the dataset.
            attrs["autojudge"] = False
            attrs["task"] = task
            dataset = Dataset(**attrs)
            self.sql_session.add(dataset)

        except Exception as error:
            logger.warning("Invalid field.", exc_info=True)
            self.application.service.add_notification(
                make_datetime(), "Invalid field(s)", repr(error))
            self.redirect(fallback_page)
            return

        if original_dataset is not None:
            # If we were cloning the dataset, copy all managers and
            # testcases across too. If the user insists, clone all
            # evaluation information too.
            clone_results = bool(self.get_argument("clone_results", False))
            dataset.clone_from(original_dataset, True, True, clone_results)

        # If the task does not yet have an active dataset, make this
        # one active.
        if task.active_dataset is None:
            task.active_dataset = dataset

        if self.try_commit():
            self.redirect("/task/%s" % task_id)
        else:
            self.redirect(fallback_page)


class RenameDatasetHandler(BaseHandler):
    """Rename the descripton of a dataset.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        self.r_params = self.render_params()
        self.r_params["task"] = task
        self.r_params["dataset"] = dataset
        self.render("rename_dataset.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, dataset_id):
        fallback_page = "/dataset/%s/rename" % dataset_id

        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        description = self.get_argument("description", "")

        # Ensure description is unique.
        if any(description == d.description
               for d in task.datasets if d is not dataset):
            self.application.service.add_notification(
                make_datetime(),
                "Dataset name \"%s\" is already taken." % description,
                "Please choose a unique name for this dataset.")
            self.redirect(fallback_page)
            return

        dataset.description = description

        if self.try_commit():
            self.redirect("/task/%s" % task.id)
        else:
            self.redirect(fallback_page)


class DeleteDatasetHandler(BaseHandler):
    """Delete a dataset from a task.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        self.r_params = self.render_params()
        self.r_params["task"] = task
        self.r_params["dataset"] = dataset
        self.render("delete_dataset.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        self.sql_session.delete(dataset)

        if self.try_commit():
            # self.application.service.scoring_service.reinitialize()
            pass
        self.redirect("/task/%s" % task.id)


class ActivateDatasetHandler(BaseHandler):
    """Set a given dataset to be the active one for a task.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        changes = compute_changes_for_dataset(task.active_dataset, dataset)
        notify_participations = set()

        # By default, we will notify users who's public scores have changed, or
        # their non-public scores have changed but they have used a token.
        for c in changes:
            score_changed = c.old_score is not None or c.new_score is not None
            public_score_changed = c.old_public_score is not None or \
                c.new_public_score is not None
            if public_score_changed or \
                    (c.submission.tokened() and score_changed):
                notify_participations.add(c.submission.participation.id)

        self.r_params = self.render_params()
        self.r_params["task"] = task
        self.r_params["dataset"] = dataset
        self.r_params["changes"] = changes
        self.r_params["default_notify_participations"] = notify_participations
        self.render("activate_dataset.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        task.active_dataset = dataset

        if self.try_commit():
            self.application.service.proxy_service.dataset_updated(
                task_id=task.id)

            # This kicks off judging of any submissions which were previously
            # unloved, but are now part of an autojudged taskset.
            self.application.service\
                .evaluation_service.search_operations_not_done()
            self.application.service\
                .scoring_service.search_operations_not_done()

        # Now send notifications to contestants.
        datetime = make_datetime()

        r = re.compile('notify_([0-9]+)$')
        count = 0
        for k in self.request.arguments:
            m = r.match(k)
            if not m:
                continue
            participation = self.safe_get_item(Participation, m.group(1))
            message = Message(datetime,
                              self.get_argument("message_subject", ""),
                              self.get_argument("message_text", ""),
                              participation=participation)
            self.sql_session.add(message)
            count += 1

        if self.try_commit():
            self.application.service.add_notification(
                make_datetime(),
                "Messages sent to %d users." % count, "")

        self.redirect("/task/%s" % task.id)


class ToggleAutojudgeDatasetHandler(BaseHandler):
    """Toggle whether a given dataset is judged automatically or not.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)

        dataset.autojudge = not dataset.autojudge

        if self.try_commit():
            # self.application.service.scoring_service.reinitialize()

            # This kicks off judging of any submissions which were previously
            # unloved, but are now part of an autojudged taskset.
            self.application.service\
                .evaluation_service.search_operations_not_done()
            self.application.service\
                .scoring_service.search_operations_not_done()

        self.redirect("/task/%s" % dataset.task_id)


class AddManagerHandler(BaseHandler):
    """Add a manager to a dataset.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        self.r_params = self.render_params()
        self.r_params["task"] = task
        self.r_params["dataset"] = dataset
        self.render("add_manager.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, dataset_id):
        fallback_page = "/dataset/%s/managers/add" % dataset_id

        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        manager = self.request.files["manager"][0]
        task_name = task.name
        self.sql_session.close()

        try:
            digest = self.application.service.file_cacher.put_file_content(
                manager["body"],
                "Task manager for %s" % task_name)
        except Exception as error:
            self.application.service.add_notification(
                make_datetime(),
                "Manager storage failed",
                repr(error))
            self.redirect(fallback_page)
            return

        self.sql_session = Session()
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        manager = Manager(manager["filename"], digest, dataset=dataset)
        self.sql_session.add(manager)

        if self.try_commit():
            self.redirect("/task/%s" % task.id)
        else:
            self.redirect(fallback_page)


class DeleteManagerHandler(BaseHandler):
    """Delete a manager.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id, manager_id):
        manager = self.safe_get_item(Manager, manager_id)
        dataset = self.safe_get_item(Dataset, dataset_id)

        # Protect against URLs providing incompatible parameters.
        if manager.dataset is not dataset:
            raise tornado.web.HTTPError(404)

        task = manager.dataset.task

        self.sql_session.delete(manager)

        self.try_commit()
        self.redirect("/task/%s" % task.id)


class AddTestcaseHandler(BaseHandler):
    """Add a testcase to a dataset.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        self.r_params = self.render_params()
        self.r_params["task"] = task
        self.r_params["dataset"] = dataset
        self.render("add_testcase.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, dataset_id):
        fallback_page = "/dataset/%s/testcases/add" % dataset_id

        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        codename = self.get_argument("codename")

        try:
            input_ = self.request.files["input"][0]
            output = self.request.files["output"][0]
        except KeyError:
            self.application.service.add_notification(
                make_datetime(),
                "Invalid data",
                "Please fill both input and output.")
            self.redirect(fallback_page)
            return

        public = self.get_argument("public", None) is not None
        task_name = task.name
        self.sql_session.close()

        try:
            input_digest = \
                self.application.service.file_cacher.put_file_content(
                    input_["body"],
                    "Testcase input for task %s" % task_name)
            output_digest = \
                self.application.service.file_cacher.put_file_content(
                    output["body"],
                    "Testcase output for task %s" % task_name)
        except Exception as error:
            self.application.service.add_notification(
                make_datetime(),
                "Testcase storage failed",
                repr(error))
            self.redirect(fallback_page)
            return

        self.sql_session = Session()
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        testcase = Testcase(
            codename, public, input_digest, output_digest, dataset=dataset)
        self.sql_session.add(testcase)

        if self.try_commit():
            # max_score and/or extra_headers might have changed.
            self.application.service.proxy_service.reinitialize()
            self.redirect("/task/%s" % task.id)
        else:
            self.redirect(fallback_page)


class AddTestcasesHandler(BaseHandler):
    """Add several testcases to a dataset.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id):
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        self.r_params = self.render_params()
        self.r_params["task"] = task
        self.r_params["dataset"] = dataset
        self.render("add_testcases.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, dataset_id):
        fallback_page = "/dataset/%s/testcases/add_multiple" % dataset_id

        # TODO: this method is quite long, some splitting is needed.
        dataset = self.safe_get_item(Dataset, dataset_id)
        task = dataset.task

        try:
            archive = self.request.files["archive"][0]
        except KeyError:
            self.application.service.add_notification(
                make_datetime(),
                "Invalid data",
                "Please choose tests archive.")
            self.redirect(fallback_page)
            return

        public = self.get_argument("public", None) is not None
        overwrite = self.get_argument("overwrite", None) is not None

        # Get input/output file names templates, or use default ones.
        input_template = self.get_argument("input_template", None)
        if not input_template:
            input_template = "input.*"
        output_template = self.get_argument("output_template", None)
        if not output_template:
            output_template = "output.*"
        input_re = re.compile(re.escape(input_template).replace("\\*",
                              "(.*)") + "$")
        output_re = re.compile(re.escape(output_template).replace("\\*",
                               "(.*)") + "$")

        task_name = task.name
        self.sql_session.close()

        fp = StringIO(archive["body"])
        try:
            with zipfile.ZipFile(fp, "r") as archive_zfp:
                tests = dict()
                # Match input/output file names to testcases' codenames.
                for filename in archive_zfp.namelist():
                    match = input_re.match(filename)
                    if match:
                        codename = match.group(1)
                        if codename not in tests:
                            tests[codename] = [None, None]
                        tests[codename][0] = filename
                    else:
                        match = output_re.match(filename)
                        if match:
                            codename = match.group(1)
                            if codename not in tests:
                                tests[codename] = [None, None]
                            tests[codename][1] = filename

                skipped_tc = []
                overwritten_tc = []
                added_tc = []
                for codename, testdata in tests.iteritems():
                    # If input or output file isn't found, skip it.
                    if not testdata[0] or not testdata[1]:
                        continue
                    self.sql_session = Session()

                    # Check, whether current testcase already exists.
                    dataset = self.safe_get_item(Dataset, dataset_id)
                    task = dataset.task
                    if codename in dataset.testcases:
                        # If we are allowed, remove existing testcase.
                        # If not - skip this testcase.
                        if overwrite:
                            testcase = dataset.testcases[codename]
                            self.sql_session.delete(testcase)

                            if not self.try_commit():
                                skipped_tc.append(codename)
                                continue
                            overwritten_tc.append(codename)
                        else:
                            skipped_tc.append(codename)
                            continue

                    # Add current testcase.
                    try:
                        input_ = archive_zfp.read(testdata[0])
                        output = archive_zfp.read(testdata[1])
                    except Exception as error:
                        self.application.service.add_notification(
                            make_datetime(),
                            "Reading testcase %s failed" % codename,
                            repr(error))
                        self.redirect(fallback_page)
                        return
                    try:
                        input_digest = self.application.service\
                            .file_cacher.put_file_content(
                                input_,
                                "Testcase input for task %s" % task_name)
                        output_digest = self.application.service\
                            .file_cacher.put_file_content(
                                output,
                                "Testcase output for task %s" % task_name)
                    except Exception as error:
                        self.application.service.add_notification(
                            make_datetime(),
                            "Testcase storage failed",
                            repr(error))
                        self.redirect(fallback_page)
                        return
                    testcase = Testcase(codename, public, input_digest,
                                        output_digest, dataset=dataset)
                    self.sql_session.add(testcase)

                    if not self.try_commit():
                        self.application.service.add_notification(
                            make_datetime(),
                            "Couldn't add test %s" % codename,
                            "")
                        self.redirect(fallback_page)
                        return
                    if codename not in overwritten_tc:
                        added_tc.append(codename)
        except zipfile.BadZipfile:
            self.application.service.add_notification(
                make_datetime(),
                "The selected file is not a zip file.",
                "Please select a valid zip file.")
            self.redirect(fallback_page)
            return

        self.application.service.add_notification(
            make_datetime(),
            "Successfully added %d and overwritten %d testcase(s)" %
            (len(added_tc), len(overwritten_tc)),
            "Added: %s; overwritten: %s; skipped: %s" %
            (", ".join(added_tc) if added_tc else "none",
             ", ".join(overwritten_tc) if overwritten_tc else "none",
             ", ".join(skipped_tc) if skipped_tc else "none"))
        self.application.service.proxy_service.reinitialize()
        self.redirect("/task/%s" % task.id)


class DeleteTestcaseHandler(BaseHandler):
    """Delete a testcase.

    """
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, dataset_id, testcase_id):
        testcase = self.safe_get_item(Testcase, testcase_id)
        dataset = self.safe_get_item(Dataset, dataset_id)

        # Protect against URLs providing incompatible parameters.
        if dataset is not testcase.dataset:
            raise tornado.web.HTTPError(404)

        task = testcase.dataset.task

        self.sql_session.delete(testcase)

        if self.try_commit():
            # max_score and/or extra_headers might have changed.
            self.application.service.proxy_service.reinitialize()
        self.redirect("/task/%s" % task.id)
