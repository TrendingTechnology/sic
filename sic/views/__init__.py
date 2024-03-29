import urllib.request
from http import HTTPStatus
import datetime
import hashlib
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
)
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.contrib import messages
from django.core.paginator import Paginator, InvalidPage
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_http_methods, condition
from django.db.models import Max
from ..models import Story, StoryKind, Comment, User, Invitation, Domain
from ..moderation import ModerationLogEntry
from ..forms import (
    SubmitCommentForm,
    EditReplyForm,
    DeleteCommentForm,
    SubmitStoryForm,
    SearchCommentsForm,
    BanUserForm,
)
from ..apps import SicAppConfig as config
from ..markdown import comment_to_html
from ..search import query_comments, query_stories


class HttpResponseNotImplemented(HttpResponse):
    status_code = HTTPStatus.NOT_IMPLEMENTED


def form_errors_as_string(errors):
    return ", ".join(
        map(lambda k: ", ".join(map(lambda err: k + ": " + err, errors[k])), errors)
    )


from .account import *
from .tags import *


@login_required
def preview_comment(request):
    if "next" not in request.GET:
        return HttpResponseBadRequest("Request url should have a ?next= GET parameter.")
    if "comment_preview" in request.session:
        request.session["comment_preview"] = {}
    # The preview is saved in the user's session, and then retrieved in
    # the template by the tag `get_comment_preview` located in
    # sic/templatetags/utils.py which puts the preview in the rendering
    # context.
    comment = None
    try:
        comment_pk = request.POST.get("preview_comment_pk", None)
        if comment_pk:
            comment = Comment.objects.get(pk=comment_pk)
        else:
            comment_pk = "null"
        text = request.POST["text"]
        request.session["comment_preview"] = {comment_pk: {}}
        request.session["comment_preview"][comment_pk]["input"] = text
        request.session["comment_preview"][comment_pk]["html"] = comment_to_html(text)
        request.session.modified = True
        if comment:
            return redirect(request.GET["next"] + "#" + comment.slugify())
        else:
            return redirect(request.GET["next"])
    except (Comment.DoesNotExist, KeyError):
        return redirect(request.GET["next"])


def story(request, story_pk, slug=None):
    try:
        story_obj = Story.objects.get(pk=story_pk)
    except Story.DoesNotExist:
        raise Http404("Story does not exist") from Story.DoesNotExist
    ongoing_reply_pk = None
    try:
        comment_pk = next(iter(request.session["comment_preview"].keys()))
        comment_pk = comment_pk if comment_pk == "null" else int(comment_pk)
        ongoing_reply_pk = comment_pk
    except (StopIteration, KeyError, ValueError):
        pass
    if request.method == "POST":
        form = SubmitCommentForm(request.POST)
        if not request.user.is_authenticated:
            messages.add_message(
                request, messages.ERROR, "You must be logged in to comment."
            )
        elif not request.user.has_perm("sic.add_comment"):
            if request.user.banned_by_user is not None:
                messages.add_message(
                    request,
                    messages.ERROR,
                    "You are banned and not allowed to comment.",
                )
            else:
                messages.add_message(
                    request, messages.ERROR, "You are not allowed to comment."
                )
        else:
            if form.is_valid():
                comment = Comment.objects.create(
                    user=request.user,
                    story=story_obj,
                    parent=None,
                    text=form.cleaned_data["text"],
                )
                request.session["comment_preview"] = {}
                return redirect(comment)
            else:
                error = form_errors_as_string(form.errors)
                messages.add_message(
                    request, messages.ERROR, f"Invalid comment form. Error: {error}"
                )
    else:
        if slug != story_obj.slugify():
            return redirect(story_obj.get_absolute_url())
        form = SubmitCommentForm()
    comments = story_obj.comments.filter(parent=None)
    return render(
        request,
        "posts/story.html",
        {
            "story": story_obj,
            "comment_form": form,
            "comments": comments,
            "ongoing_reply_pk": ongoing_reply_pk,
        },
    )


@login_required
def reply(request, comment_pk):
    user = request.user
    try:
        comment = Comment.objects.get(pk=comment_pk)
    except Comment.DoesNotExist:
        raise Http404("Comment does not exist") from Comment.DoesNotExist
    if request.method == "POST":
        form = SubmitCommentForm(request.POST)
        if not user.has_perm("sic.add_comment"):
            if user.banned_by_user is not None:
                messages.add_message(
                    request,
                    messages.ERROR,
                    "You are banned and not allowed to comment.",
                )
            else:
                messages.add_message(
                    request, messages.ERROR, "You are not allowed to comment."
                )
        elif form.is_valid():
            text = form.cleaned_data["text"]
            comment = Comment.objects.create(
                user=user, story=comment.story, parent=comment, text=text
            )
            request.session["comment_preview"] = {}
            return redirect(comment)
        else:
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
    return redirect(comment.story.get_absolute_url())


def agg_etag_fn(request, taggregation_pk, slug, page_num=1):
    try:
        agg = Taggregation.objects.get(pk=taggregation_pk)
        m = hashlib.sha256()
        m.update(bytes(str(agg.last_modified.timestamp()), "utf-8"))
        m.update(bytes(str(taggregation_pk), "utf-8"))
        last_active = agg.last_active()
        if last_active:
            m.update(bytes(str(last_active.timestamp()), "utf-8"))
        if request.user.is_authenticated:
            m.update(bytes(request.user.get_session_auth_hash(), "utf-8"))
            latest = Notification.latest(request.user)
            if latest:
                m.update(bytes(str(latest.timestamp()), "utf-8"))
        return m.hexdigest()

    except Taggregation.DoesNotExist:
        raise Http404("Taggregation does not exist") from Taggregation.DoesNotExist


def agg_last_modified_fn(request, taggregation_pk, slug, page_num=1):
    try:
        agg = Taggregation.objects.get(pk=taggregation_pk)
        last_modified = agg.last_modified
        last_active = agg.last_active() or last_modified
        notifications_active = last_active
        if request.user.is_authenticated:
            latest = Notification.latest(request.user)
            if latest:
                notifications_active = latest
        return max(last_modified, last_active, notifications_active)
    except Taggregation.DoesNotExist:
        raise Http404("Taggregation does not exist") from Taggregation.DoesNotExist


@condition(etag_func=agg_etag_fn, last_modified_func=agg_last_modified_fn)
def agg_index(request, taggregation_pk, slug, page_num=1):
    if page_num == 1 and request.get_full_path() != reverse(
        "agg_index", kwargs={"taggregation_pk": taggregation_pk, "slug": slug}
    ):
        return redirect(
            reverse(
                "agg_index", kwargs={"taggregation_pk": taggregation_pk, "slug": slug}
            )
        )
    try:
        agg = Taggregation.objects.get(pk=taggregation_pk)
    except Taggregation.DoesNotExist:
        raise Http404("Taggregation does not exist") from Taggregation.DoesNotExist
    if slug != agg.slugify():
        return redirect(agg.get_absolute_url())
    if not agg.user_has_access(request.user):
        if request.user.is_authenticated:
            raise Http404("Taggregation does not exist") from Taggregation.DoesNotExist
        else:
            return redirect(
                reverse("login")
                + "?"
                + urlencode(
                    {
                        "next": reverse(
                            "agg_index_page",
                            kwargs={
                                "taggregation_pk": taggregation_pk,
                                "slug": slug,
                                "page_num": page_num,
                            },
                        )
                    }
                )
            )
    stories = agg.get_stories()
    # https://docs.python.org/3/howto/sorting.html#sort-stability-and-complex-sorts
    all_stories = sorted(
        stories,
        key=lambda s: s.title,
        reverse=True,
    )
    all_stories = sorted(
        all_stories,
        key=lambda s: s.created,
    )
    all_stories = sorted(
        all_stories,
        key=lambda s: s.hotness()["score"],
        reverse=True,
    )
    paginator = Paginator(all_stories, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse(
                "agg_index_page",
                kwargs={
                    "taggregation_pk": taggregation_pk,
                    "slug": slug,
                    "page_num": paginator.num_pages,
                },
            )
        )
    return render(
        request,
        "index.html",
        {
            "stories": page,
            "has_subscriptions": True,
            "aggregation": agg,
        },
    )


def index_etag_fn(request, page_num=1):
    # This resource depends on the following:
    #
    # ├─ notification count in nav menu (if new messages arrive or existing messages are read, the count changes)
    # ├─ last_modified of all aggregations (name, description etc)
    # ├─ last_active of all stories of all aggregations
    # └─ last_modified of all tags of all aggregations (name, hex_color) (currently ignored/not computed)
    #

    default = True
    last_modifieds = None
    last_actives = None
    notifications_active = None
    m = hashlib.sha256()

    if request.user.is_authenticated:
        m.update(bytes(request.user.get_session_auth_hash(), "utf-8"))
        latest = Notification.latest(request.user)
        if latest:
            notifications_active = latest
        taggregations = request.user.taggregation_subscriptions.all()
        if taggregations.exists():
            default = False
            last_actives = Taggregation.last_actives(taggregations)
            last_modifieds = taggregations.aggregate(m=Max("last_modified"))["m"]

    if default:
        taggregations = Taggregation.objects.filter(default=True)
        if not taggregations.exists():
            latest = Story.objects.filter(active=True).latest("last_active")
            last_actives = latest.last_active if latest else make_aware(datetime.now())
        last_actives = Taggregation.last_actives(taggregations)
        last_modifieds = taggregations.aggregate(m=Max("last_modified"))["m"]

    if last_actives:
        m.update(bytes(str(last_actives.timestamp()), "utf-8"))
    if last_modifieds:
        m.update(bytes(str(last_modifieds.timestamp()), "utf-8"))
    if notifications_active:
        m.update(bytes(str(notifications_active.timestamp()), "utf-8"))

    return m.hexdigest()


def index_last_modified_fn(request, page_num=1):
    default = True
    notifications_active = None

    if request.user.is_authenticated:
        latest = Notification.latest(request.user)
        if latest:
            notifications_active = latest
        taggregations = request.user.taggregation_subscriptions.all()
        if taggregations.exists():
            default = False
            last_actives = Taggregation.last_actives(taggregations)
            last_modifieds = taggregations.aggregate(m=Max("last_modified"))["m"]

    if default:
        taggregations = Taggregation.objects.filter(default=True)
        if not taggregations.exists():
            latest = Story.objects.filter(active=True).latest("last_active")
            return latest.last_active if latest else make_aware(datetime.now())
        last_actives = Taggregation.last_actives(taggregations)
        last_modifieds = taggregations.aggregate(m=Max("last_modified"))["m"]
        notifications_active = last_actives or last_modifieds

    return (
        max(last_modifieds, last_actives, notifications_active)
        if notifications_active
        else None
    )


@condition(etag_func=index_etag_fn, last_modified_func=index_last_modified_fn)
def index(request, page_num=1):
    if page_num == 1 and request.get_full_path() != reverse("index"):
        # Redirect to '/' to avoid having both '/' and '/page/1' as valid urls.
        return redirect(reverse("index"))
    stories = None
    has_subscriptions = False
    # Figure out what to show in the index
    # If user is authenticated AND has subscribed aggregations, show the set union of them
    # otherwise show every story.
    if request.user.is_authenticated:
        frontpage = request.user.frontpage()
        has_subscriptions = frontpage["taggregations"] is not None
    else:
        frontpage = Taggregation.default_frontpage()
    stories = frontpage["stories"]
    taggregations = frontpage["taggregations"]
    # https://docs.python.org/3/howto/sorting.html#sort-stability-and-complex-sorts
    all_stories = sorted(
        stories,
        key=lambda s: s.title,
        reverse=True,
    )
    all_stories = sorted(
        all_stories,
        key=lambda s: s.created,
    )
    all_stories = sorted(
        all_stories,
        key=lambda s: s.hotness()["score"],
        reverse=True,
    )
    paginator = Paginator(all_stories, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(reverse("index_page", kwargs={"page_num": paginator.num_pages}))
    return render(
        request,
        "index.html",
        {
            "stories": page,
            "has_subscriptions": has_subscriptions,
            "aggregations": taggregations,
        },
    )


def all_stories_etag_fn(request, page_num=1):
    # This resource depends on the following:
    #
    # ├─ notification count in nav menu (if new messages arrive or existing messages are read, the count changes)
    # ├─ last_active of all stories
    # └─ last_modified of all tags of all stories (name, hex_color) (currently ignored/not computed)
    #

    m = hashlib.sha256()

    if request.user.is_authenticated:
        m.update(bytes(request.user.get_session_auth_hash(), "utf-8"))
        latest = Notification.latest(request.user)
        if latest:
            m.update(bytes(str(latest.timestamp()), "utf-8"))

    latest = Story.objects.filter(active=True).latest("last_active")
    if latest:
        m.update(bytes(str(latest.last_active.timestamp()), "utf-8"))

    return m.hexdigest()


def all_stories_last_modified_fn(request, page_num=1):
    notifications_active = None

    if request.user.is_authenticated:
        l = Notification.latest(request.user)
        if l:
            notifications_active = l
    latest = Story.objects.filter(active=True).latest("last_active")
    latest = latest.last_active if latest else None

    if notifications_active and latest:
        return max(latest, notifications_active)
    elif notifications_active:
        return notifications_active
    else:
        return latest


@condition(
    etag_func=all_stories_etag_fn, last_modified_func=all_stories_last_modified_fn
)
def all_stories(request, page_num=1):
    if "order_by" in request.GET:
        request.session["all_stories_order_by"] = request.GET["order_by"]
    if "ordering" in request.GET:
        request.session["all_stories_ordering"] = request.GET["ordering"]

    if page_num == 1 and request.get_full_path() != reverse("all_stories"):
        return redirect(reverse("all_stories"))

    order_by = request.session.get("all_stories_order_by", "hotness")
    ordering = request.session.get("all_stories_ordering", "desc")
    order_by_field = ("-" if ordering == "desc" else "") + order_by

    story_obj = Story.objects.filter(active=True)
    if order_by == "hotness":
        stories = sorted(
            story_obj.order_by("created", "title"),
            key=lambda s: s.hotness()["score"],
            reverse=ordering == "desc",
        )
    elif order_by == "last commented":
        stories = sorted(
            story_obj.order_by("created", "title"),
            key=lambda s: s.active_comments().latest("created").created
            if s.active_comments().exists()
            else s.created,
            reverse=ordering == "desc",
        )
    else:
        stories = story_obj.order_by(order_by_field, "title")

    paginator = Paginator(stories, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse("all_stories_page", kwargs={"page_num": paginator.num_pages})
        )
    order_by_form = OrderByForm(
        fields=all_stories.ORDER_BY_FIELDS,
        initial={"order_by": order_by, "ordering": ordering},
    )
    return render(
        request,
        "posts/all_stories.html",
        {"stories": page, "order_by_form": order_by_form},
    )


all_stories.ORDER_BY_FIELDS = ["hotness", "created", "last commented"]


@login_required
def submit_story(request):
    user = request.user
    preview = None
    if request.method == "POST":
        if "fetch-title" in request.POST:
            qdict = request.POST.copy()
            if len(qdict["url"]) > 0:
                try:
                    with urllib.request.urlopen(qdict["url"], timeout=2) as r:
                        text = r.read().decode("utf-8")
                    parsr = TitleHTMLExtractor()
                    parsr.feed(text)
                    title = parsr.title
                    qdict["title"] = title
                    if parsr.ogtitle is not None:
                        qdict["title"] = parsr.ogtitle
                    if parsr.publish_date is not None:
                        qdict["publish_date"] = parsr.publish_date
                except Exception as exc:
                    messages.add_message(
                        request, messages.ERROR, f"Could not fetch title. Error: {exc}"
                    )
            else:
                messages.add_message(request, messages.WARNING, "URL field is empty.")
            form = SubmitStoryForm(qdict)
        elif "preview" in request.POST:
            form = SubmitStoryForm(request.POST)
            form.is_valid()
            preview = {
                "description": comment_to_html(request.POST["description"]),
                "title": form.cleaned_data["title"],
                "url": form.cleaned_data["url"],
                "domain": form.cleaned_data["url"]
                if len(form.cleaned_data["url"]) > 0
                else None,
                "publish_date": form.cleaned_data["publish_date"],
                "tags": form.cleaned_data["tags"],
            }
        else:
            form = SubmitStoryForm(request.POST)
            form.fields["title"].required = True
            if not user.has_perm("sic.add_story"):
                if user.banned_by_user is not None:
                    messages.add_message(
                        request,
                        messages.ERROR,
                        "You are banned and not allowed to submit stories.",
                    )
                else:
                    messages.add_message(
                        request,
                        messages.ERROR,
                        "You are not allowed to submit stories.",
                    )
            elif form.is_valid():
                title = form.cleaned_data["title"]
                description = form.cleaned_data["description"]
                url = form.cleaned_data["url"]
                publish_date = form.cleaned_data["publish_date"]
                user_is_author = form.cleaned_data["user_is_author"]
                new_story = Story.objects.create(
                    title=title,
                    url=url,
                    publish_date=publish_date,
                    description=description,
                    user=user,
                    user_is_author=user_is_author,
                    context_warning=form.cleaned_data["context_warning"],
                )
                new_story.tags.set(form.cleaned_data["tags"])
                new_story.kind.set(form.cleaned_data["kind"])
                new_story.save()
                return redirect(new_story.get_absolute_url())
            form.fields["title"].required = False
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
    else:
        form = SubmitStoryForm(initial={"kind": StoryKind.default_value()})
    return render(
        request,
        "posts/submit.html",
        {
            "form": form,
            "preview": preview,
        },
    )


@login_required
def upvote_story(request, story_pk):
    if request.method == "POST":
        user = request.user
        try:
            story_obj = Story.objects.get(pk=story_pk)
        except Story.DoesNotExist:
            raise Http404("Story does not exist") from Story.DoesNotExist
        if story_obj.user.pk == user.pk:
            messages.add_message(
                request, messages.ERROR, "You cannot vote on your own posts."
            )
        else:
            vote, created = user.votes.get_or_create(
                story=story_obj, comment=None, user=user
            )
            if not created:
                vote.delete()
    if "next" in request.GET:
        return redirect(request.GET["next"])
    return redirect(reverse("index"))


@login_required
def edit_comment(request, comment_pk):
    user = request.user
    try:
        comment_obj = Comment.objects.get(pk=comment_pk)
    except Comment.DoesNotExist:
        raise Http404("Comment does not exist") from Comment.DoesNotExist

    if (
        not request.user.is_admin
        and not request.user.is_moderator
        and request.user != comment_obj.user
    ):
        raise PermissionDenied("Only a comment author or a moderator may edit it.")

    if request.method == "POST":
        form = EditReplyForm(request.POST)
        if form.is_valid():
            before_text = comment_obj.text
            reason = form.cleaned_data["edit_reason"] or ""
            if comment_obj.user != request.user and not reason:
                messages.add_message(
                    request,
                    messages.ERROR,
                    "A reason is required in order to edit someone else's comment!",
                )
            else:
                ModerationLogEntry.edit_comment(
                    before_text, comment_obj, request.user, reason
                )
                comment_obj.text = form.cleaned_data["text"]
                comment_obj.save()
                return redirect(comment_obj)
            return redirect(comment_obj.story.get_absolute_url())
        else:
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
            return redirect(comment_obj.story.get_absolute_url())
    else:
        form = EditReplyForm(
            initial={
                "text": comment_obj.text,
            },
        )
        return render(
            request,
            "posts/edit_comment.html",
            {
                "story": comment_obj.story,
                "comment": comment_obj,
                "form": form,
            },
        )


@login_required
def delete_comment(request, comment_pk):
    user = request.user
    try:
        comment_obj = Comment.objects.get(pk=comment_pk)
    except Comment.DoesNotExist:
        raise Http404("Comment does not exist") from Comment.DoesNotExist

    if (
        not request.user.is_admin
        and not request.user.is_moderator
        and request.user != comment_obj.user
    ):
        raise PermissionDenied(
            "Only a comment author or a moderator may delete this comment."
        )

    if request.method == "POST":
        form = DeleteCommentForm(request.POST)
        if form.is_valid():
            comment_obj.deleted = True
            reason = form.cleaned_data["deletion_reason"] or ""
            if comment_obj.user != request.user and not reason:
                messages.add_message(
                    request,
                    messages.ERROR,
                    "A reason is required in order to delete someone else's comment!",
                )
            else:
                ModerationLogEntry.delete_comment(
                    comment_obj, request.user, form.cleaned_data["deletion_reason"]
                )
                comment_obj.save()
                messages.add_message(
                    request,
                    messages.SUCCESS,
                    f"Your comment (id: {comment_obj.pk}) has been deleted.",
                )
        else:
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
    else:
        form = DeleteCommentForm()
        return render(
            request,
            "posts/edit_comment.html",
            {
                "story": comment_obj.story,
                "comment": comment_obj,
                "edit_comment_pk": comment_pk,
                "delete_comment": True,
                "delete_comment_form": form,
            },
        )

    return redirect(comment_obj.story.get_absolute_url())


@login_required
def edit_story(request, story_pk, slug=None):
    user = request.user
    preview = None
    try:
        story_obj = Story.objects.get(pk=story_pk)
    except Story.DoesNotExist:
        raise Http404("Story does not exist") from Story.DoesNotExist
    if not request.user.has_perm("sic.change_story", story_obj):
        raise PermissionDenied("Only the author of the story can edit it.")
    if request.method == "POST":
        if "fetch-title" in request.POST:
            qdict = request.POST.copy()
            if len(qdict["url"]) > 0:
                try:
                    with urllib.request.urlopen(qdict["url"], timeout=2) as r:
                        text = r.read().decode("utf-8")
                    parsr = TitleHTMLExtractor()
                    parsr.feed(text)
                    title = parsr.title
                    qdict["title"] = title
                    if parsr.ogtitle is not None:
                        qdict["title"] = parsr.ogtitle
                    if parsr.publish_date is not None:
                        qdict["publish_date"] = parsr.publish_date
                except Exception as exc:
                    messages.add_message(
                        request, messages.ERROR, f"Could not fetch title. Error: {exc}"
                    )
            else:
                messages.add_message(request, messages.WARNING, "URL field is empty.")
            form = SubmitStoryForm(qdict)
        elif "preview" in request.POST:
            form = SubmitStoryForm(request.POST)
            form.is_valid()
            preview = {
                "description": comment_to_html(request.POST["description"]),
                "title": form.cleaned_data["title"],
                "url": form.cleaned_data["url"],
                "domain": form.cleaned_data["url"],
                "publish_date": form.cleaned_data["publish_date"],
                "tags": form.cleaned_data["tags"],
            }
        else:
            form = SubmitStoryForm(request.POST)
            if form.is_valid():
                story_obj.title = form.cleaned_data["title"]
                story_obj.description = form.cleaned_data["description"]
                story_obj.url = form.cleaned_data["url"]
                story_obj.user_is_author = form.cleaned_data["user_is_author"]
                story_obj.tags.set(form.cleaned_data["tags"])
                story_obj.kind.set(form.cleaned_data["kind"])
                story_obj.publish_date = form.cleaned_data["publish_date"]
                story_obj.context_warning = form.cleaned_data["context_warning"]
                story_obj.save()
                return redirect(story_obj.get_absolute_url())
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
    else:
        form = SubmitStoryForm(
            initial={
                "title": story_obj.title,
                "description": story_obj.description,
                "url": story_obj.url,
                "publish_date": story_obj.publish_date,
                "user_is_author": story_obj.user_is_author,
                "tags": story_obj.tags.all(),
                "kind": story_obj.kind.all(),
                "context_warning": story_obj.context_warning,
            }
        )
    return render(
        request,
        "posts/submit.html",
        {
            "form": form,
            "preview": preview,
            "story": story_obj,
        },
    )


@login_required
def upvote_comment(request, story_pk, slug, comment_pk):
    if request.method == "POST":
        user = request.user
        try:
            story_obj = Story.objects.get(pk=story_pk)
        except Story.DoesNotExist:
            raise Http404("Story does not exist") from Story.DoesNotExist
        try:
            comment_obj = Comment.objects.get(pk=comment_pk)
        except Comment.DoesNotExist:
            raise Http404("Comment does not exist") from Comment.DoesNotExist
        if comment_obj.user.pk == user.pk:
            messages.add_message(
                request, messages.ERROR, "You cannot vote on your own posts."
            )
        else:
            vote, created = user.votes.filter(story=story_obj).get_or_create(
                story=story_obj, comment=comment_obj, user=user
            )
            if not created:
                vote.delete()
    if "next" in request.GET:
        return redirect(request.GET["next"])
    return redirect(reverse("index"))


def recent_comments_etag_fn(request, page_num=1):
    # This resource depends on the following:
    #
    # ├─ notification count in nav menu (if new messages arrive or existing messages are read, the count changes)
    # └─ last_active of all stories of all comments
    #

    m = hashlib.sha256()

    if request.user.is_authenticated:
        m.update(bytes(request.user.get_session_auth_hash(), "utf-8"))
        latest = Notification.latest(request.user)
        if latest:
            m.update(bytes(str(latest.timestamp()), "utf-8"))

    latest = Comment.objects.filter(deleted=False).latest("story__last_active")
    if latest:
        m.update(bytes(str(latest.story.last_active.timestamp()), "utf-8"))

    return m.hexdigest()


def recent_comments_last_modified_fn(request, page_num=1):
    notifications_active = None

    if request.user.is_authenticated:
        l = Notification.latest(request.user)
        if l:
            notifications_active = l
    latest = Comment.objects.filter(deleted=False).latest("story__last_active")
    latest = latest.story.last_active if latest else None

    if notifications_active and latest:
        return max(latest, notifications_active)
    elif notifications_active:
        return notifications_active
    else:
        return latest


@condition(
    etag_func=recent_comments_etag_fn,
    last_modified_func=recent_comments_last_modified_fn,
)
def recent_comments(request, page_num=1):
    if page_num == 1 and request.get_full_path() != reverse("recent_comments"):
        # Redirect to '/' to avoid having both '/' and '/page/1' as valid urls.
        return redirect(reverse("recent_comments"))
    comments = Comment.objects.order_by("-created").filter(deleted=False)
    paginator = Paginator(comments[:40], config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse("recent_comments_page", kwargs={"page_num": paginator.num_pages})
        )
    return render(request, "posts/recent_comments.html", {"comments": page})


def invitation_tree(request):
    root_user = User.objects.earliest("created")

    return render(
        request,
        "about/about_invitation_tree.html",
        {
            "root_user": root_user,
            "depth": 0,
            "max_depth": 64,
        },
    )


@require_http_methods(["GET"])
def comment_source(request, story_pk, slug, comment_pk):
    try:
        story_obj = Story.objects.get(pk=story_pk)
    except Story.DoesNotExist:
        raise Http404("Story does not exist") from Story.DoesNotExist
    try:
        comment_obj = Comment.objects.get(pk=comment_pk)
    except Comment.DoesNotExist:
        raise Http404("Comment does not exist") from Comment.DoesNotExist
    return HttpResponse(comment_obj.text, content_type="text/plain; charset=utf-8")


@require_http_methods(["GET"])
def search(request):
    comments = None
    stories = None
    if "text" in request.GET:
        form = SearchCommentsForm(request.GET)
        if form.is_valid():
            if form.cleaned_data["search_in"] in ["comments", "both"]:
                comments = query_comments(form.cleaned_data["text"])
            if form.cleaned_data["search_in"] in ["stories", "both"]:
                stories = query_stories(form.cleaned_data["text"])
    else:
        form = SearchCommentsForm()
    return render(
        request,
        "posts/search.html",
        {"form": form, "comments": comments, "stories": stories},
    )


from html.parser import HTMLParser


class TitleHTMLExtractor(HTMLParser):
    def __init__(self):
        super(TitleHTMLExtractor, self).__init__()
        self.title = ""
        self.ogtitle = None
        self.publish_date = None
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            attrs = {a[0]: a[1] for a in attrs}
            if (
                "content" in attrs
                and "property" in attrs
                and attrs["property"] == "article:published_time"
            ):
                try:
                    if attrs["content"].endswith("Z"):
                        attrs["content"] = attrs["content"][:-1]
                    self.publish_date = datetime.fromisoformat(attrs["content"]).date()
                except:
                    pass
            if (
                "content" in attrs
                and "property" in attrs
                and attrs["property"] == "og:title"
            ):
                self.ogtitle = attrs["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


@require_http_methods(["GET"])
def moderation_log(request, page_num=1):
    if page_num == 1 and request.get_full_path() != reverse("moderation_log"):
        return redirect(reverse("moderation_log"))
    logs = ModerationLogEntry.objects.order_by("-action_time")
    paginator = Paginator(logs, 10)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse("moderation_log_page", kwargs={"page_num": paginator.num_pages})
        )
    return render(
        request,
        "moderation/moderation_log.html",
        {
            "logs": page,
        },
    )


@login_required
def moderation(request):
    if (not request.user.is_moderator) and (not request.user.is_admin):
        raise PermissionDenied("You are not a moderator.")
    if request.method == "POST":
        if "set-ban" in request.POST:
            ban_user_form = BanUserForm(request.POST)
            if ban_user_form.is_valid():
                user = ban_user_form.cleaned_data["username"]
                ban = ban_user_form.cleaned_data["ban"]
                reason = ban_user_form.cleaned_data["reason"]
                if ban and user.banned_by_user is not None:
                    messages.add_message(
                        request, messages.ERROR, f"{user} already banned"
                    )
                    return redirect(reverse("moderation"))
                if not ban and user.banned_by_user is None:
                    messages.add_message(
                        request, messages.ERROR, f"{user} already not banned"
                    )
                    return redirect(reverse("moderation"))
                user.banned_by_user = request.user if ban else None
                user.save()
                log_entry = ModerationLogEntry.changed_user_status(
                    user,
                    request.user,
                    "Banned user" if ban else "Unbanned user",
                    reason,
                )
                messages.add_message(
                    request, messages.SUCCESS, f"{log_entry.action} {user}"
                )
                return redirect(reverse("moderation"))
            error = form_errors_as_string(ban_user_form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
        else:
            ban_user_form = BanUserForm()
    else:
        ban_user_form = BanUserForm()
    return render(
        request, "moderation/moderation.html", {"ban_user_form": ban_user_form}
    )


def domain(request, slug, page_num=1):
    try:
        domain_obj = Domain.objects.get(url=Domain.deslugify(slug))
    except Domain.DoesNotExist:
        raise Http404("Domain does not exist") from Domain.DoesNotExist
    if "order_by" in request.GET:
        request.session["domain_order_by"] = request.GET["order_by"]
    if "ordering" in request.GET:
        request.session["domain_ordering"] = request.GET["ordering"]
    if page_num == 1 and request.get_full_path() != reverse("domain", args=[slug]):
        return redirect(reverse("domain", args=[slug]))
    order_by = request.session.get("domain_order_by", "created")
    if order_by not in domain.ORDER_BY_FIELDS:
        order_by = domain.ORDER_BY_FIELDS[0]
    ordering = request.session.get("domain_ordering", "desc")
    order_by_field = ("-" if ordering == "desc" else "") + order_by
    stories = Story.objects.filter(active=True, domain=domain_obj).order_by(
        order_by_field
    )
    paginator = Paginator(stories, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse(
                "domain_page",
                args=[slug],
                kwargs={
                    "page_num": paginator.num_pages,
                },
            )
        )

    order_by_form = OrderByForm(
        fields=domain.ORDER_BY_FIELDS,
        initial={"order_by": order_by, "ordering": ordering},
    )
    return render(
        request,
        "posts/all_stories.html",
        {"stories": page, "order_by_form": order_by_form, "domain": domain_obj},
    )


domain.ORDER_BY_FIELDS = ["created", "title"]
