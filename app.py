import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
import warnings
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
import click
from flask_wtf import FlaskForm
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import (
    FileField,
    HiddenField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    EqualTo,
    InputRequired,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    full_name TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    email TEXT COLLATE NOCASE UNIQUE,
    student_number TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    avatar_filename TEXT,
    approval_status TEXT NOT NULL DEFAULT 'approved'
        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approval_requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approval_decided_at TEXT,
    certificate_filename TEXT,
    banned_at TEXT,
    banned_by INTEGER REFERENCES users(id),
    ban_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS registration_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL CHECK (position >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, position)
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES users(id),
    listing_type TEXT NOT NULL CHECK (listing_type IN ('product', 'service')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    category TEXT NOT NULL,
    stock INTEGER NOT NULL CHECK (stock >= 0),
    image_filename TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'sold', 'removed')),
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK (availability IN ('available', 'unavailable')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_id INTEGER NOT NULL REFERENCES users(id),
    seller_id INTEGER NOT NULL REFERENCES users(id),
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
    total_price_cents INTEGER NOT NULL
        CHECK (total_price_cents = unit_price_cents * quantity),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'confirmed', 'preparing', 'ready_for_pickup',
            'completed', 'cancelled'
        )),
    status_reason TEXT NOT NULL DEFAULT '',
    order_type TEXT NOT NULL DEFAULT 'purchase'
        CHECK (order_type IN ('purchase', 'preorder')),
    stock_reserved INTEGER NOT NULL DEFAULT 1 CHECK (stock_reserved IN (0, 1)),
    payment_method TEXT NOT NULL DEFAULT 'cash'
        CHECK (payment_method IN ('cash', 'gcash', 'maya')),
    request_token TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (buyer_id, request_token)
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    buyer_id INTEGER NOT NULL REFERENCES users(id),
    seller_id INTEGER NOT NULL REFERENCES users(id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    feedback TEXT NOT NULL CHECK (length(trim(feedback)) > 0 AND length(feedback) <= 2000),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS listing_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    reporter_id INTEGER NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'pending', 'resolved', 'dismissed')),
    resolved_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    resolution_details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    customer_id INTEGER NOT NULL REFERENCES users(id),
    provider_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (customer_id != provider_id),
    UNIQUE (listing_id, customer_id, provider_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    sender_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 2000),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS support_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    user_last_read_message_id INTEGER NOT NULL DEFAULT 0,
    admin_last_read_message_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved')),
    resolved_by INTEGER REFERENCES users(id),
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES support_conversations(id),
    sender_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 2000),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL REFERENCES users(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL CHECK (kind IN ('message', 'order', 'account')),
    body TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id),
    order_id INTEGER REFERENCES orders(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at TEXT,
    CHECK (
        (kind = 'message' AND conversation_id IS NOT NULL AND order_id IS NULL)
        OR (kind = 'order' AND order_id IS NOT NULL AND conversation_id IS NULL)
        OR (kind = 'account' AND conversation_id IS NULL AND order_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS admin_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    scope TEXT NOT NULL,
    client_fingerprint TEXT NOT NULL,
    window_started INTEGER NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    PRIMARY KEY (scope, client_fingerprint)
);

CREATE INDEX IF NOT EXISTS listings_browse_idx
    ON listings(status, category, listing_type, created_at);
CREATE INDEX IF NOT EXISTS listings_seller_idx ON listings(seller_id, status);
CREATE INDEX IF NOT EXISTS orders_seller_status_idx ON orders(seller_id, status);
CREATE INDEX IF NOT EXISTS orders_buyer_status_idx ON orders(buyer_id, status);
CREATE INDEX IF NOT EXISTS orders_listing_status_idx
    ON orders(listing_id, status);
CREATE INDEX IF NOT EXISTS reviews_seller_idx ON reviews(seller_id);
CREATE INDEX IF NOT EXISTS reports_status_idx ON listing_reports(status, created_at);
CREATE INDEX IF NOT EXISTS messages_conversation_idx
    ON messages(conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS support_messages_conversation_idx
    ON support_messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS notifications_recipient_idx
    ON notifications(recipient_id, read_at, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_report_per_user_listing
    ON listing_reports(listing_id, reporter_id)
    WHERE status IN ('open', 'pending');
"""

PRODUCT_CATEGORIES = (
    "Books",
    "Clothing",
    "Electronics",
    "Food & Drinks",
    "School Supplies",
    "Other",
)
SERVICE_CATEGORIES = ("Services",)
LISTING_CATEGORIES = PRODUCT_CATEGORIES + SERVICE_CATEGORIES
MARKETPLACE_PAGE_SIZE = 24
NOTIFICATION_PAGE_SIZE = 20
ADMIN_USERS_PAGE_SIZE = 25
ADMIN_RECORDS_PAGE_SIZE = 25
CATEGORY_LABELS = {
    "Food & Drinks": "Foods and Drinks",
    "Other": "Others",
}
ORDER_STATUSES = (
    "pending",
    "confirmed",
    "preparing",
    "ready_for_pickup",
    "completed",
    "cancelled",
)
ORDER_PROGRESS_STATUSES = (
    "pending",
    "confirmed",
    "preparing",
    "ready_for_pickup",
    "completed",
)
REPORT_REASONS = (
    ("inappropriate", "Inappropriate content"),
    ("scam", "Suspected scam or misleading listing"),
    ("prohibited", "Prohibited or unsafe item"),
    ("incorrect", "Incorrect listing information"),
    ("spam", "Spam or duplicate listing"),
    ("other", "Other"),
)
ALLOWED_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
AVATAR_MIMETYPES_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
AVATAR_FILENAME_PATTERN = r"^[0-9a-f]{32}\.png$"
LISTING_FILENAME_PATTERN = AVATAR_FILENAME_PATTERN
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_LISTING_IMAGE_BYTES = 4 * 1024 * 1024
MAX_CERTIFICATE_BYTES = 4 * 1024 * 1024
MAX_CERTIFICATE_IMAGES = 5
PHILIPPINE_TIMEZONE = timezone(timedelta(hours=8))
MAX_AVATAR_PIXELS = 20_000_000
PREORDER_STOCK_THRESHOLD = 0


class RegistrationForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[DataRequired(message="Username is required."), Length(max=50)],
    )
    student_number = StringField(
        "Student Number",
        validators=[
            DataRequired(message="Student number is required."),
            Length(max=30, message="Student number must be 30 characters or fewer."),
        ],
    )
    full_name = StringField(
        "Full name",
        validators=[DataRequired(message="Full name is required."), Length(max=120)],
    )
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(min=8, message="Use at least 8 characters for your password."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm password",
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo("password", message="The passwords do not match."),
        ],
    )
    certificate = FileField("Registration Form and ID")
    submit = SubmitField("Create account")


class LoginForm(FlaskForm):
    username_or_email = StringField(
        "Username or email",
        validators=[DataRequired(message="Username or email is required.")],
    )
    password = PasswordField(
        "Password", validators=[DataRequired(message="Password is required.")]
    )
    submit = SubmitField("Sign in")


class LogoutForm(FlaskForm):
    submit = SubmitField("Sign out")


class BanUserForm(FlaskForm):
    reason = TextAreaField(
        "Reason for ban",
        validators=[
            DataRequired(message="Enter a reason for the ban."),
            Length(max=500, message="The reason must be 500 characters or fewer."),
        ],
    )
    submit = SubmitField("Ban account")


class RegistrationDecisionForm(FlaskForm):
    reason = TextAreaField(
        "Rejection reason",
        validators=[Optional(), Length(max=500, message="The reason must be 500 characters or fewer.")],
    )
    submit = SubmitField("Confirm decision")


class ProfileForm(FlaskForm):
    full_name = StringField(
        "Full name",
        validators=[
            DataRequired(message="Full name is required."),
            Length(max=120, message="Full name must be 120 characters or fewer."),
        ],
    )
    bio = TextAreaField(
        "Bio",
        validators=[
            Optional(),
            Length(max=300, message="Bio must be 300 characters or fewer."),
        ],
    )
    submit = SubmitField("Save changes")


class ProfilePhotoForm(FlaskForm):
    photo = FileField("Profile photo")
    submit = SubmitField("Upload photo")


class RemovePhotoForm(FlaskForm):
    submit = SubmitField("Remove profile photo")


def validate_price(form, field):
    value = (field.data or "").strip()
    try:
        price = Decimal(value)
    except InvalidOperation as error:
        raise ValidationError("Enter a valid price.") from error
    if not price.is_finite():
        raise ValidationError("Enter a valid price.")
    if price <= 0:
        raise ValidationError("Price must be greater than zero.")
    if not re.fullmatch(r"(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?", value):
        raise ValidationError("Enter a price with up to two decimal places.")


class ListingForm(FlaskForm):
    listing_type = SelectField(
        "Listing type",
        choices=[
            ("product", "Product"),
            ("service", "Service (contact only)"),
        ],
        default="product",
        validators=[DataRequired(message="Choose a listing type.")],
    )
    title = StringField(
        "Title",
        validators=[
            DataRequired(message="Title is required."),
            Length(max=120, message="Title must be 120 characters or fewer."),
        ],
    )
    description = TextAreaField(
        "Description",
        validators=[
            DataRequired(message="Description is required."),
            Length(max=5000, message="Description must be 5,000 characters or fewer."),
        ],
    )
    price = StringField(
        "Price (PHP)", validators=[DataRequired(message="Price is required."), validate_price]
    )
    category = SelectField(
        "Category",
        choices=[("", "Choose a category")]
        + [
            (category, CATEGORY_LABELS.get(category, category))
            for category in LISTING_CATEGORIES
        ],
        validators=[DataRequired(message="Choose a category.")],
    )
    stock = IntegerField(
        "Available quantity",
        validators=[
            Optional(),
            NumberRange(min=0, message="Available quantity cannot be negative."),
        ],
    )
    image = FileField("Listing image (optional)")
    submit = SubmitField("Publish listing")

    def validate_category(self, field):
        categories = (
            SERVICE_CATEGORIES
            if self.listing_type.data == "service"
            else PRODUCT_CATEGORIES
        )
        if field.data in LISTING_CATEGORIES and field.data not in categories:
            raise ValidationError("Choose a category that matches the listing type.")

    def validate_stock(self, field):
        if self.listing_type.data == "product" and (
            field.data is None or 0 <= field.data < 1
        ):
            raise ValidationError("Enter at least one available unit for a product.")


class PlaceOrderForm(FlaskForm):
    quantity = IntegerField(
        "Quantity",
        default=1,
        validators=[
            InputRequired(message="Quantity is required."),
            NumberRange(min=1, message="Quantity must be at least one."),
        ],
    )
    payment_method = SelectField(
        "Payment method",
        choices=[("cash", "Cash"), ("gcash", "GCash"), ("maya", "Maya")],
        default="cash",
        validators=[DataRequired(message="Choose a payment method.")],
    )
    request_token = HiddenField(validators=[DataRequired()])
    submit = SubmitField("Place order")


class CancelOrderForm(FlaskForm):
    submit = SubmitField("Cancel order")


class OrderStatusForm(FlaskForm):
    status = SelectField("Update status", choices=[])
    status_reason = TextAreaField(
        "Reason (optional)",
        validators=[Length(max=500, message="Reason must be 500 characters or fewer.")],
    )
    submit = SubmitField("Update status")


class ReviewForm(FlaskForm):
    rating = SelectField(
        "Rating",
        choices=[(str(value), f"{value} star{'s' if value != 1 else ''}") for value in range(1, 6)],
        validators=[DataRequired(message="Choose a rating from 1 to 5.")],
    )
    feedback = TextAreaField(
        "Feedback",
        validators=[
            DataRequired(message="Feedback is required."),
            Length(max=2000, message="Feedback must be 2,000 characters or fewer."),
        ],
    )
    submit = SubmitField("Submit rating")


class ReportForm(FlaskForm):
    reason = SelectField(
        "Reason",
        choices=[("", "Choose a reason")] + list(REPORT_REASONS),
        validators=[DataRequired(message="Choose a report reason.")],
    )
    explanation = TextAreaField(
        "Additional details (optional)",
        validators=[Length(max=2000, message="Details must be 2,000 characters or fewer.")],
    )
    submit = SubmitField("Submit report")


class MessageForm(FlaskForm):
    body = TextAreaField(
        "Message",
        validators=[
            DataRequired(message="Enter a message."),
            Length(max=2000, message="Messages must be 2,000 characters or fewer."),
        ],
    )
    submit = SubmitField("Send message")


class SupportConversationActionForm(FlaskForm):
    pass


class ContactProviderForm(FlaskForm):
    submit = SubmitField("Contact seller")


class NotificationActionForm(FlaskForm):
    pass


class ResolveReportForm(FlaskForm):
    resolution_details = TextAreaField(
        "Resolution details",
        validators=[
            Length(max=2000, message="Resolution details must be 2,000 characters or fewer.")
        ],
    )
    submit = SubmitField("Mark resolved")


class ArchiveListingForm(FlaskForm):
    submit = SubmitField("Archive listing")


class RestockListingForm(FlaskForm):
    quantity = IntegerField(
        "Units to add",
        validators=[
            InputRequired(message="Enter a restock quantity."),
            NumberRange(min=1, max=1_000_000, message="Enter a quantity from 1 to 1,000,000."),
        ],
    )
    submit = SubmitField("Restock")


class ListingAvailabilityForm(FlaskForm):
    availability = SelectField(
        "Availability",
        choices=[("available", "Available"), ("unavailable", "Unavailable")],
        validators=[DataRequired(message="Choose a valid availability status.")],
    )
    submit = SubmitField("Update availability")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    database = Path(current_app.config["DATABASE"])
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as db:
        db.executescript(SCHEMA)
        notification_schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()[0]
        if "'account'" not in notification_schema:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                CREATE TABLE notifications_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient_id INTEGER NOT NULL REFERENCES users(id),
                    actor_id INTEGER NOT NULL REFERENCES users(id),
                    kind TEXT NOT NULL CHECK (kind IN ('message', 'order', 'account')),
                    body TEXT NOT NULL,
                    conversation_id INTEGER REFERENCES conversations(id),
                    order_id INTEGER REFERENCES orders(id),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    read_at TEXT,
                    CHECK (
                        (kind = 'message' AND conversation_id IS NOT NULL AND order_id IS NULL)
                        OR (kind = 'order' AND order_id IS NOT NULL AND conversation_id IS NULL)
                        OR (kind = 'account' AND conversation_id IS NULL AND order_id IS NULL)
                    )
                )
                """
            )
            db.execute(
                """
                INSERT INTO notifications_new
                    (id, recipient_id, actor_id, kind, body, conversation_id,
                     order_id, created_at, read_at)
                SELECT id, recipient_id, actor_id, kind, body, conversation_id,
                       order_id, created_at, read_at
                FROM notifications
                """
            )
            db.execute("DROP TABLE notifications")
            db.execute("ALTER TABLE notifications_new RENAME TO notifications")
            db.execute(
                """
                CREATE INDEX notifications_recipient_idx
                ON notifications(recipient_id, read_at, created_at DESC)
                """
            )
            db.commit()
        support_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(support_conversations)").fetchall()
        }
        if "status" not in support_columns:
            db.execute(
                """
                ALTER TABLE support_conversations ADD COLUMN status TEXT NOT NULL
                    DEFAULT 'open' CHECK (status IN ('open', 'resolved'))
                """
            )
        if "resolved_by" not in support_columns:
            db.execute(
                """
                ALTER TABLE support_conversations ADD COLUMN resolved_by INTEGER
                    REFERENCES users(id)
                """
            )
        if "resolved_at" not in support_columns:
            db.execute(
                "ALTER TABLE support_conversations ADD COLUMN resolved_at TEXT"
            )
        listing_columns = {
            row[1] for row in db.execute("PRAGMA table_info(listings)").fetchall()
        }
        if "availability" not in listing_columns:
            db.execute(
                """
                ALTER TABLE listings ADD COLUMN availability TEXT NOT NULL
                    DEFAULT 'available'
                    CHECK (availability IN ('available', 'unavailable'))
                """
            )
        report_columns = {
            row[1] for row in db.execute("PRAGMA table_info(listing_reports)").fetchall()
        }
        report_schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'listing_reports'"
        ).fetchone()[0]
        if (
            "resolution_details" not in report_columns
            or "'dismissed'" not in report_schema
            or "'open'" not in report_schema
        ):
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP INDEX IF EXISTS one_pending_report_per_user_listing")
            db.execute("DROP INDEX IF EXISTS one_open_report_per_user_listing")
            db.execute(
                """
                CREATE TABLE listing_reports_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id INTEGER NOT NULL REFERENCES listings(id),
                    reporter_id INTEGER NOT NULL REFERENCES users(id),
                    reason TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'pending', 'resolved', 'dismissed')),
                    resolved_by INTEGER REFERENCES users(id),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT,
                    resolution_details TEXT NOT NULL DEFAULT ''
                )
                """
            )
            resolution_details = (
                "resolution_details" if "resolution_details" in report_columns else "''"
            )
            db.execute(
                f"""
                INSERT INTO listing_reports_new
                    (id, listing_id, reporter_id, reason, explanation, status,
                     resolved_by, created_at, resolved_at, resolution_details)
                SELECT id, listing_id, reporter_id, reason, explanation, status,
                       resolved_by, created_at, resolved_at, {resolution_details}
                FROM listing_reports
                """
            )
            db.execute("DROP TABLE listing_reports")
            db.execute("ALTER TABLE listing_reports_new RENAME TO listing_reports")
            db.execute(
                "CREATE INDEX reports_status_idx ON listing_reports(status, created_at)"
            )
            db.execute(
                """
                CREATE UNIQUE INDEX one_open_report_per_user_listing
                ON listing_reports(listing_id, reporter_id)
                WHERE status IN ('open', 'pending')
                """
            )
            db.commit()
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()
        }
        if "role" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'"
            )
        if "avatar_filename" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN avatar_filename TEXT")
        if "bio" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
        if "approval_status" not in columns:
            db.execute(
                """
                ALTER TABLE users ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'approved'
                    CHECK (approval_status IN ('pending', 'approved', 'rejected'))
                """
            )
        if "approval_requested_at" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN approval_requested_at TEXT")
        if "approval_decided_at" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN approval_decided_at TEXT")
        db.execute(
            """
            UPDATE users
            SET approval_requested_at = created_at
            WHERE approval_requested_at IS NULL OR approval_requested_at = ''
            """
        )
        db.execute(
            """
            UPDATE users
            SET approval_decided_at = (
                SELECT MAX(created_at)
                FROM admin_actions
                WHERE target_type = 'student_registration'
                  AND target_id = users.id
                  AND action IN (
                      'approved_student_registration',
                      'rejected_student_registration'
                  )
            )
            WHERE approval_status IN ('approved', 'rejected')
              AND approval_decided_at IS NULL
            """
        )
        db.commit()
        if "certificate_filename" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN certificate_filename TEXT")
        if "student_number" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN student_number TEXT")
        if "banned_at" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
        if "banned_by" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN banned_by INTEGER REFERENCES users(id)"
            )
        if "ban_reason" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN ban_reason TEXT NOT NULL DEFAULT ''"
            )
        email_column = next(
            (row for row in db.execute("PRAGMA table_info(users)") if row[1] == "email"),
            None,
        )
        if email_column is not None and email_column[3]:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    full_name TEXT NOT NULL,
                    bio TEXT NOT NULL DEFAULT '',
                    email TEXT COLLATE NOCASE UNIQUE,
                    student_number TEXT,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'student',
                    avatar_filename TEXT,
                    approval_status TEXT NOT NULL DEFAULT 'approved'
                        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
                    approval_requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    approval_decided_at TEXT,
                    certificate_filename TEXT,
                    banned_at TEXT,
                    banned_by INTEGER REFERENCES users(id),
                    ban_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                INSERT INTO users_new
                    (id, username, full_name, bio, email, student_number,
                     password_hash, role,
                     avatar_filename, approval_status, approval_requested_at,
                     approval_decided_at, certificate_filename,
                     banned_at, banned_by, ban_reason, created_at)
                SELECT id, username, full_name, bio, email, student_number,
                       password_hash, role,
                       avatar_filename, approval_status, approval_requested_at,
                       approval_decided_at, certificate_filename,
                       banned_at, banned_by, ban_reason, created_at
                FROM users
                """
            )
            db.execute("DROP TABLE users")
            db.execute("ALTER TABLE users_new RENAME TO users")
            db.commit()
            db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_student_number_unique
            ON users(student_number COLLATE NOCASE)
            WHERE student_number IS NOT NULL AND student_number != ''
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS users_pending_review_idx
            ON users(approval_status, approval_requested_at)
            """
        )
        order_schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()[0]
        if "'confirmed'" not in order_schema:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP INDEX IF EXISTS orders_seller_status_idx")
            db.execute("DROP INDEX IF EXISTS orders_buyer_status_idx")
            db.execute(
                """
                CREATE TABLE orders_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    buyer_id INTEGER NOT NULL REFERENCES users(id),
                    seller_id INTEGER NOT NULL REFERENCES users(id),
                    listing_id INTEGER NOT NULL REFERENCES listings(id),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
                    total_price_cents INTEGER NOT NULL
                        CHECK (total_price_cents = unit_price_cents * quantity),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'confirmed', 'completed', 'cancelled')),
                    payment_method TEXT NOT NULL DEFAULT 'cash'
                        CHECK (payment_method IN ('cash', 'gcash', 'maya')),
                    request_token TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (buyer_id, request_token)
                )
                """
            )
            db.execute(
                """
                INSERT INTO orders_new
                    (id, buyer_id, seller_id, listing_id, quantity, unit_price_cents,
                     total_price_cents, status, request_token, created_at)
                SELECT id, buyer_id, seller_id, listing_id, quantity, unit_price_cents,
                       total_price_cents, status, request_token, created_at
                FROM orders
                """
            )
            db.execute("DROP TABLE orders")
            db.execute("ALTER TABLE orders_new RENAME TO orders")
            db.execute(
                "CREATE INDEX orders_seller_status_idx ON orders(seller_id, status)"
            )
            db.execute(
                "CREATE INDEX orders_buyer_status_idx ON orders(buyer_id, status)"
            )
        order_columns = {
            row[1] for row in db.execute("PRAGMA table_info(orders)").fetchall()
        }
        if "payment_method" not in order_columns:
            db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'cash'
                    CHECK (payment_method IN ('cash', 'gcash', 'maya'))
                """
            )
        if "order_type" not in order_columns:
            db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN order_type TEXT NOT NULL DEFAULT 'purchase'
                    CHECK (order_type IN ('purchase', 'preorder'))
                """
            )
        if "stock_reserved" not in order_columns:
            db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN stock_reserved INTEGER NOT NULL DEFAULT 1
                    CHECK (stock_reserved IN (0, 1))
                """
            )
        if "status_reason" not in order_columns:
            db.execute(
                "ALTER TABLE orders ADD COLUMN status_reason TEXT NOT NULL DEFAULT ''"
            )
        order_schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()[0]
        if (
            "'preparing'" not in order_schema
            or "'ready_for_pickup'" not in order_schema
            or "'maya'" not in order_schema
        ):
            db.commit()
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                CREATE TABLE orders_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    buyer_id INTEGER NOT NULL REFERENCES users(id),
                    seller_id INTEGER NOT NULL REFERENCES users(id),
                    listing_id INTEGER NOT NULL REFERENCES listings(id),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
                    total_price_cents INTEGER NOT NULL
                        CHECK (total_price_cents = unit_price_cents * quantity),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'confirmed', 'preparing', 'ready_for_pickup',
                            'completed', 'cancelled'
                        )),
                    status_reason TEXT NOT NULL DEFAULT '',
                    order_type TEXT NOT NULL DEFAULT 'purchase'
                        CHECK (order_type IN ('purchase', 'preorder')),
                    stock_reserved INTEGER NOT NULL DEFAULT 1
                        CHECK (stock_reserved IN (0, 1)),
                    payment_method TEXT NOT NULL DEFAULT 'cash'
                        CHECK (payment_method IN ('cash', 'gcash', 'maya')),
                    request_token TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (buyer_id, request_token)
                )
                """
            )
            db.execute(
                """
                INSERT INTO orders_new
                    (id, buyer_id, seller_id, listing_id, quantity, unit_price_cents,
                     total_price_cents, status, status_reason, order_type,
                     stock_reserved, payment_method, request_token, created_at)
                SELECT id, buyer_id, seller_id, listing_id, quantity, unit_price_cents,
                       total_price_cents, status, status_reason, order_type,
                       stock_reserved, payment_method, request_token, created_at
                FROM orders
                """
            )
            db.execute("DROP TABLE orders")
            db.execute("ALTER TABLE orders_new RENAME TO orders")
            db.execute(
                "CREATE INDEX orders_seller_status_idx ON orders(seller_id, status)"
            )
            db.execute(
                "CREATE INDEX orders_buyer_status_idx ON orders(buyer_id, status)"
            )
            db.commit()
            db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            """
            INSERT OR IGNORE INTO registration_documents (user_id, filename, position)
            SELECT id, certificate_filename, 0
            FROM users
            WHERE certificate_filename IS NOT NULL AND certificate_filename != ''
            """
        )
        db.commit()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if session.get("user_id") is None:
            flash("Please sign in to view that page.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped_view(*args, **kwargs):
        user = get_db().execute(
            "SELECT role FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        if user is None:
            session.clear()
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login"))
        if user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped_view


def delete_avatar_file(filename):
    if not filename or not re.fullmatch(AVATAR_FILENAME_PATTERN, filename):
        return
    path = Path(current_app.config["UPLOAD_FOLDER"]) / filename
    try:
        path.unlink(missing_ok=True)
    except OSError:
        current_app.logger.exception("Could not remove profile photo file.")


def delete_certificate_file(filename):
    if not filename or not re.fullmatch(AVATAR_FILENAME_PATTERN, filename):
        return
    path = Path(current_app.config["CERTIFICATE_UPLOAD_FOLDER"]) / filename
    try:
        path.unlink(missing_ok=True)
    except OSError:
        current_app.logger.exception("Could not remove enrollment certificate file.")


def remove_registration_files(filenames):
    folder = Path(current_app.config["CERTIFICATE_UPLOAD_FOLDER"])
    removed_files = {}
    try:
        for filename in dict.fromkeys(filenames):
            if not filename:
                continue
            if not re.fullmatch(AVATAR_FILENAME_PATTERN, filename):
                raise ValueError("A registration document filename is invalid.")
            path = folder / filename
            try:
                contents = path.read_bytes()
            except FileNotFoundError:
                continue
            path.unlink()
            removed_files[path] = contents
    except (OSError, ValueError):
        restore_registration_files(removed_files)
        raise
    return removed_files


def restore_registration_files(removed_files):
    for path, contents in removed_files.items():
        try:
            if not path.exists():
                path.write_bytes(contents)
        except OSError:
            current_app.logger.exception(
                "Could not restore a registration document after a failed decision."
            )


def avatar_is_available(filename):
    if not filename or not re.fullmatch(AVATAR_FILENAME_PATTERN, filename):
        return False
    return (Path(current_app.config["UPLOAD_FOLDER"]) / filename).is_file()


def avatar_size_limit_label():
    return format_file_size(current_app.config["MAX_AVATAR_BYTES"])


def format_money(price_cents):
    return f"₱{Decimal(price_cents) / 100:,.2f}"


def format_datetime(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    return parsed.strftime("%b %d, %Y · %I:%M %p")


def normalize_order_action(action):
    if action == "declined":
        return "cancelled"
    if action in {
        "confirmed",
        "preparing",
        "ready_for_pickup",
        "completed",
        "cancelled",
    }:
        return action
    return None


def available_order_actions(order, actor_id, is_admin=False):
    seller = actor_id == order["seller_id"]
    buyer = actor_id == order["buyer_id"]
    status = order["status"]
    if status == "pending":
        actions = [("confirmed", "Confirm Order")] if seller else []
        if seller:
            actions.append(("cancelled", "Reject Order"))
        if buyer or is_admin:
            actions.append(("cancelled", "Cancel Order"))
        return actions
    if status == "confirmed":
        actions = (
            [
                ("preparing", "Mark as Preparing"),
                ("completed", "Mark as Completed"),
            ]
            if seller
            else []
        )
        if buyer or is_admin:
            actions.append(("cancelled", "Cancel Order"))
        return actions
    if status == "preparing":
        actions = [("ready_for_pickup", "Mark as Ready for Pickup")] if seller else []
        if is_admin:
            actions.append(("cancelled", "Cancel Order"))
        return actions
    if status == "ready_for_pickup" and buyer:
        return [("completed", "Confirm Receipt & Complete")]
    if status == "ready_for_pickup" and is_admin:
        return [("cancelled", "Cancel Order")]
    return []


def listing_image_is_available(filename):
    if not filename or not re.fullmatch(LISTING_FILENAME_PATTERN, filename):
        return False
    path = Path(current_app.config["LISTING_UPLOAD_FOLDER"]) / filename
    return path.is_file()


def listing_image_size_limit_label():
    return format_file_size(current_app.config["MAX_LISTING_IMAGE_BYTES"])


def format_file_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:g} KB"
    return f"{size_bytes / (1024 * 1024):g} MB"


def create_notification(
    db, recipient_id, actor_id, kind, body, *, conversation_id=None, order_id=None
):
    db.execute(
        """
        INSERT INTO notifications
            (recipient_id, actor_id, kind, body, conversation_id, order_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (recipient_id, actor_id, kind, body, conversation_id, order_id),
    )


def csrf_token_is_valid(form):
    csrf_field = getattr(form, "csrf_token", None)
    return csrf_field is None or csrf_field.validate(form, extra_validators=[])


AUTH_RATE_LIMITS = {
    "login": (5, 15 * 60),
    "registration": (5, 60 * 60),
}


def auth_rate_limit(scope, *, record_attempt):
    limit, window_seconds = AUTH_RATE_LIMITS[scope]
    client_ip = request.remote_addr or "unknown"
    fingerprint = hmac.new(
        current_app.secret_key.encode("utf-8"),
        client_ip.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    now = int(time.time())
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "DELETE FROM auth_rate_limits WHERE window_started <= ?",
        (now - max(window for _, window in AUTH_RATE_LIMITS.values()),),
    )
    row = db.execute(
        """
        SELECT window_started, attempts
        FROM auth_rate_limits
        WHERE scope = ? AND client_fingerprint = ?
        """,
        (scope, fingerprint),
    ).fetchone()
    if row is not None and now - row["window_started"] < window_seconds:
        retry_after = max(1, row["window_started"] + window_seconds - now)
        if row["attempts"] >= limit:
            db.commit()
            return False, retry_after
        if record_attempt:
            db.execute(
                """
                UPDATE auth_rate_limits
                SET attempts = attempts + 1
                WHERE scope = ? AND client_fingerprint = ?
                """,
                (scope, fingerprint),
            )
    elif record_attempt:
        db.execute(
            """
            INSERT INTO auth_rate_limits
                (scope, client_fingerprint, window_started, attempts)
            VALUES (?, ?, ?, 1)
            ON CONFLICT (scope, client_fingerprint) DO UPDATE SET
                window_started = excluded.window_started,
                attempts = excluded.attempts
            """,
            (scope, fingerprint, now),
        )
    db.commit()
    return True, 0


def is_safe_internal_url(value):
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return False
    parsed = urlsplit(value)
    return not parsed.scheme and not parsed.netloc


def encode_next_url(value):
    return quote(value, safe="")


def delete_listing_image_file(filename):
    if not filename or not re.fullmatch(LISTING_FILENAME_PATTERN, filename):
        return
    path = Path(current_app.config["LISTING_UPLOAD_FOLDER"]) / filename
    try:
        path.unlink(missing_ok=True)
    except OSError:
        current_app.logger.exception("Could not remove listing image file.")


def decode_avatar(data):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > MAX_AVATAR_PIXELS:
                raise ValueError("Image dimensions exceed the allowed limit.")
            source.verify()
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1024, 1024))
            if "A" in image.getbands():
                normalized = image.convert("RGBA")
            else:
                normalized = image.convert("RGB")
            output = BytesIO()
            normalized.save(output, format="PNG", optimize=True)
            return output.getvalue()


def normalize_avatar_upload(photo):
    if not photo or not photo.filename:
        return None, None
    extension = Path(photo.filename).suffix.lower()
    if (
        extension not in ALLOWED_AVATAR_EXTENSIONS
        or photo.mimetype != AVATAR_MIMETYPES_BY_EXTENSION[extension]
    ):
        return None, "Upload a JPG, PNG, or WEBP image."

    data = photo.stream.read(current_app.config["MAX_AVATAR_BYTES"] + 1)
    if len(data) > current_app.config["MAX_AVATAR_BYTES"]:
        return None, f"Profile photos must be {avatar_size_limit_label()} or smaller."
    try:
        return decode_avatar(data), None
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
        UnidentifiedImageError,
    ):
        return None, "That file isn't a supported, valid image."


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    environment = os.environ.get("DANGALBASKET_ENV", "development").strip().lower()
    secret_key = os.environ.get("SECRET_KEY")
    session_cookie_secure = os.environ.get("SESSION_COOKIE_SECURE") == "1"
    if environment == "production" and not session_cookie_secure:
        raise RuntimeError(
            "SESSION_COOKIE_SECURE=1 must be set when "
            "DANGALBASKET_ENV=production."
        )
    if environment == "production" and not (
        secret_key and secret_key.strip()
    ):
        raise RuntimeError(
            "SECRET_KEY must be set in the environment when "
            "DANGALBASKET_ENV=production."
        )
    if not secret_key and not (test_config and test_config.get("SECRET_KEY")):
        secret_key = secrets.token_hex(32)
        app.logger.warning(
            "SECRET_KEY is unset; sessions will be invalidated when the app restarts."
        )
    elif not secret_key:
        secret_key = test_config["SECRET_KEY"]

    if environment == "production":
        storage_environment_variables = (
            "DANGALBASKET_DATABASE",
            "DANGALBASKET_UPLOAD_FOLDER",
            "DANGALBASKET_LISTING_UPLOAD_FOLDER",
            "DANGALBASKET_CERTIFICATE_UPLOAD_FOLDER",
        )
        project_root = Path(app.root_path).resolve()
        for variable in storage_environment_variables:
            configured_path = os.environ.get(variable)
            if not configured_path:
                raise RuntimeError(
                    f"{variable} must be set when DANGALBASKET_ENV=production."
                )
            path = Path(configured_path)
            if not path.is_absolute():
                raise RuntimeError(f"{variable} must be an absolute path.")
            try:
                path.resolve().relative_to(project_root)
            except ValueError:
                continue
            raise RuntimeError(
                f"{variable} must not point inside the application directory."
            )

    app.config.from_mapping(
        SECRET_KEY=secret_key,
        DATABASE=os.environ.get(
            "DANGALBASKET_DATABASE",
            os.path.join(app.instance_path, "dangalbasket.sqlite3"),
        ),
        UPLOAD_FOLDER=os.environ.get(
            "DANGALBASKET_UPLOAD_FOLDER",
            os.path.join(app.instance_path, "profile-photos"),
        ),
        LISTING_UPLOAD_FOLDER=os.environ.get(
            "DANGALBASKET_LISTING_UPLOAD_FOLDER",
            os.path.join(app.instance_path, "listing-images"),
        ),
        CERTIFICATE_UPLOAD_FOLDER=os.environ.get(
            "DANGALBASKET_CERTIFICATE_UPLOAD_FOLDER",
            os.path.join(app.instance_path, "enrollment-certificates"),
        ),
        MAX_AVATAR_BYTES=int(os.environ.get("MAX_AVATAR_BYTES", MAX_AVATAR_BYTES)),
        MAX_LISTING_IMAGE_BYTES=int(
            os.environ.get("MAX_LISTING_IMAGE_BYTES", MAX_LISTING_IMAGE_BYTES)
        ),
        MAX_CERTIFICATE_BYTES=int(
            os.environ.get("MAX_CERTIFICATE_BYTES", MAX_CERTIFICATE_BYTES)
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
        TEMPLATES_AUTO_RELOAD=True,
    )
    if test_config:
        app.config.update(test_config)
    if environment == "production":
        app.config["SECRET_KEY"] = secret_key
        app.config["SESSION_COOKIE_SECURE"] = True
    if "MAX_CONTENT_LENGTH" not in (test_config or {}):
        app.config["MAX_CONTENT_LENGTH"] = (
            max(
                app.config["MAX_AVATAR_BYTES"],
                app.config["MAX_LISTING_IMAGE_BYTES"],
                app.config["MAX_CERTIFICATE_BYTES"] * MAX_CERTIFICATE_IMAGES,
            )
            + 64 * 1024
        )

    app.logger.setLevel(logging.INFO)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["LISTING_UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["CERTIFICATE_UPLOAD_FOLDER"], exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()

    @app.before_request
    def invalidate_banned_account_session():
        user_id = session.get("user_id")
        if user_id is None:
            return None
        user = get_db().execute(
            """
            SELECT role, approval_status, banned_at, ban_reason,
                   CASE
                       WHEN datetime(approval_requested_at, '+48 hours')
                            <= CURRENT_TIMESTAMP THEN 1
                       ELSE 0
                   END AS review_overdue
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if user is None:
            session.clear()
        elif user["banned_at"] is not None:
            reason = user["ban_reason"]
            session.clear()
            message = "This account has been banned."
            if reason:
                message += f" Reason: {reason}"
            flash(message, "error")
        elif (
            user["role"] != "admin"
            and user["approval_status"] != "approved"
        ):
            approval_status = user["approval_status"]
            if approval_status == "rejected":
                if request.endpoint not in {
                    "notifications",
                    "open_notification",
                    "mark_all_notifications_read",
                    "register",
                    "login",
                    "logout",
                    "static",
                }:
                    flash(
                        "Your account request was not approved. You may review "
                        "the decision and resubmit your registration documents.",
                        "error",
                    )
                    return redirect(url_for("notifications"))
            else:
                if request.endpoint not in {"login", "static"}:
                    session.clear()
                    message = (
                        "Your account is still under review. Please contact the "
                        "administrator if you need assistance."
                        if user["review_overdue"]
                        else "Your account is awaiting administrator review. You "
                             "may receive an approval or rejection notification "
                             "within 48 hours."
                    )
                    flash(message, "error")
                    return redirect(url_for("login"))
        return None

    @app.context_processor
    def inject_logout_form():
        user_id = session.get("user_id")
        admin = False
        unread_notification_count = 0
        unread_message_count = 0
        if user_id is not None:
            user = get_db().execute(
                "SELECT role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            admin = user is not None and user["role"] == "admin"
            unread_notification_count = get_db().execute(
                """
                SELECT COUNT(*) FROM notifications
                WHERE recipient_id = ? AND read_at IS NULL
                """,
                (user_id,),
            ).fetchone()[0]
            unread_message_count = get_db().execute(
                """
                SELECT COUNT(*) FROM notifications
                WHERE recipient_id = ? AND kind = 'message' AND read_at IS NULL
                """,
                (user_id,),
            ).fetchone()[0]
            if admin:
                support_unread_count = get_db().execute(
                    """
                    SELECT COUNT(*)
                    FROM support_messages m
                    JOIN support_conversations c ON c.id = m.conversation_id
                    JOIN users sender ON sender.id = m.sender_id
                    WHERE sender.role != 'admin'
                      AND m.id > c.admin_last_read_message_id
                    """
                ).fetchone()[0]
            else:
                support_unread_count = get_db().execute(
                    """
                    SELECT COUNT(*)
                    FROM support_messages m
                    JOIN support_conversations c ON c.id = m.conversation_id
                    WHERE c.user_id = ? AND m.sender_id != ?
                      AND m.id > c.user_last_read_message_id
                    """,
                    (user_id, user_id),
                ).fetchone()[0]
            unread_message_count += support_unread_count
        else:
            support_unread_count = 0
        return {
            "logout_form": LogoutForm(),
            "listing_categories": LISTING_CATEGORIES,
            "category_labels": CATEGORY_LABELS,
            "order_status_form": OrderStatusForm(),
            "archive_listing_form": ArchiveListingForm(),
            "resolve_report_form": ResolveReportForm(),
            "contact_provider_form": ContactProviderForm(),
            "preorder_stock_threshold": PREORDER_STOCK_THRESHOLD,
            "current_user_is_admin": admin,
            "unread_notification_count": unread_notification_count,
            "unread_message_count": unread_message_count,
            "support_unread_count": support_unread_count,
            "listing_image_is_available": listing_image_is_available,
            "avatar_is_available": avatar_is_available,
            "is_safe_internal_url": is_safe_internal_url,
            "encode_next_url": encode_next_url,
            "certificate_size_limit": format_file_size(
                current_app.config["MAX_CERTIFICATE_BYTES"]
            ),
        }

    app.jinja_env.filters["money"] = format_money
    app.jinja_env.filters["datetime"] = format_datetime

    @app.cli.command("provision-admin")
    @click.argument("username")
    def provision_admin(username):
        """Promote one existing user when bootstrapping the first administrator."""
        expected_token = os.environ.get("ADMIN_BOOTSTRAP_TOKEN")
        if not expected_token:
            raise click.ClickException(
                "Set ADMIN_BOOTSTRAP_TOKEN in the environment before provisioning."
            )
        supplied_token = click.prompt("Bootstrap token", hide_input=True)
        if not secrets.compare_digest(supplied_token, expected_token):
            raise click.ClickException("Invalid bootstrap token.")
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            raise click.ClickException("An administrator is already provisioned.")
        user = db.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user is None:
            raise click.ClickException("No account exists with that username.")
        db.execute(
            """
            UPDATE users
            SET role = 'admin',
                approval_status = 'approved',
                approval_decided_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (user["id"],),
        )
        db.commit()
        click.echo(f"Administrator access provisioned for {username}.")

    @app.get("/healthz")
    def health_check():
        try:
            get_db().execute("SELECT 1").fetchone()
        except sqlite3.Error:
            current_app.logger.exception("Application health check failed.")
            return {"status": "unavailable"}, 503
        return {"status": "ok"}, 200

    @app.get("/")
    def index():
        if session.get("user_id") is None:
            return redirect(url_for("login"))

        user = None
        user_id = session.get("user_id")
        if user_id is not None:
            user = get_db().execute(
                "SELECT username, full_name, avatar_filename FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                session.clear()
                return redirect(url_for("login"))
        clauses = [
            "l.status IN ('active', 'sold')",
            "(l.status = 'active' OR l.stock = 0)",
        ]
        parameters = []
        if user is not None:
            clauses.append("l.seller_id != ?")
            parameters.append(user_id)
        keyword = request.args.get("q", "").strip()[:100]
        category = request.args.get("category", "")
        min_price = request.args.get("min_price", "").strip()
        max_price = request.args.get("max_price", "").strip()
        price_pattern = re.compile(r"\d{1,9}(?:\.\d{1,2})?")
        parsed_min_price = None
        parsed_max_price = None
        for field_name, raw_value in (
            ("minimum", min_price),
            ("maximum", max_price),
        ):
            if raw_value and not price_pattern.fullmatch(raw_value):
                abort(400, description=f"Enter a valid {field_name} price.")
            if raw_value:
                price_cents = int(Decimal(raw_value) * 100)
                if field_name == "minimum":
                    parsed_min_price = price_cents
                else:
                    parsed_max_price = price_cents
        if (
            parsed_min_price is not None
            and parsed_max_price is not None
            and parsed_min_price > parsed_max_price
        ):
            abort(400, description="Minimum price cannot exceed maximum price.")
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append("(l.title LIKE ? ESCAPE '\\' OR l.description LIKE ? ESCAPE '\\')")
            parameters.extend([pattern, pattern])
        if parsed_min_price is not None:
            clauses.append("l.price_cents >= ?")
            parameters.append(parsed_min_price)
        if parsed_max_price is not None:
            clauses.append("l.price_cents <= ?")
            parameters.append(parsed_max_price)
        if category in LISTING_CATEGORIES:
            clauses.append("l.category = ?")
            parameters.append(category)
        else:
            category = ""
        listing_type = request.args.get("type", "")
        if listing_type in {"product", "service"}:
            clauses.append("l.listing_type = ?")
            parameters.append(listing_type)
        else:
            listing_type = ""
        raw_page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)
        total_listings = get_db().execute(
            f"SELECT COUNT(*) FROM listings AS l WHERE {' AND '.join(clauses)}",
            parameters,
        ).fetchone()[0]
        total_pages = max(
            1,
            (total_listings + MARKETPLACE_PAGE_SIZE - 1) // MARKETPLACE_PAGE_SIZE,
        )
        if page > total_pages:
            return redirect(
                url_for(
                    "index",
                    q=keyword,
                    category=category,
                    type=listing_type,
                    min_price=min_price,
                    max_price=max_price,
                    page=total_pages,
                )
            )
        listings = get_db().execute(
            f"""
            SELECT l.*,
                   (SELECT AVG(r.rating)
                    FROM reviews AS r JOIN orders AS o ON o.id = r.order_id
                    WHERE o.listing_id = l.id) AS listing_average_rating,
                   (SELECT COUNT(*)
                    FROM reviews AS r JOIN orders AS o ON o.id = r.order_id
                    WHERE o.listing_id = l.id) AS listing_rating_count,
                   (SELECT COALESCE(SUM(o.quantity), 0)
                    FROM orders AS o
                    WHERE o.listing_id = l.id AND o.status = 'completed')
                       AS listing_sold_count
            FROM listings AS l
            WHERE {' AND '.join(clauses)}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, MARKETPLACE_PAGE_SIZE, (page - 1) * MARKETPLACE_PAGE_SIZE],
        ).fetchall()
        return render_template(
            "index.html",
            user=user,
            listings=listings,
            categories=LISTING_CATEGORIES,
            category_labels=CATEGORY_LABELS,
            filters={
                "q": keyword,
                "category": category,
                "listing_type": listing_type,
                "min_price": min_price,
                "max_price": max_price,
            },
            pagination={
                "page": page,
                "pages": total_pages,
                "total": total_listings,
            },
            logout_form=LogoutForm(),
            profile_photo_form=RemovePhotoForm(),
        )

    @app.route("/register", methods=["GET", "POST"])
    def register():
        form = RegistrationForm()
        resubmission_account = None
        if session.get("user_id") is not None:
            resubmission_account = get_db().execute(
                """
                SELECT id, username, approval_status
                FROM users WHERE id = ? AND role = 'student'
                """,
                (session["user_id"],),
            ).fetchone()
            if (
                resubmission_account is None
                or resubmission_account["approval_status"] != "rejected"
            ):
                resubmission_account = None
        if request.method == "GET" and resubmission_account:
            form.username.data = resubmission_account["username"]
        if request.method == "POST" and csrf_token_is_valid(form):
            try:
                allowed, retry_after = auth_rate_limit(
                    "registration", record_attempt=True
                )
            except sqlite3.Error:
                db = g.get("db")
                if db is not None:
                    db.rollback()
                current_app.logger.exception(
                    "Could not enforce registration rate limit."
                )
                return "Registration is temporarily unavailable.", 503
            if not allowed:
                return (
                    "Too many registration attempts. Please try again later.",
                    429,
                    {"Retry-After": str(retry_after)},
                )
        if form.validate_on_submit():
            username = (
                resubmission_account["username"]
                if resubmission_account
                else form.username.data.strip()
            )
            student_number = form.student_number.data.strip()
            if not student_number:
                form.student_number.errors.append(
                    "Student number is required."
                )
                return render_template(
                    "register.html",
                    form=form,
                    resubmission=bool(resubmission_account),
                ), 400
            full_name = form.full_name.data.strip()
            certificates = [
                upload
                for upload in request.files.getlist("certificate")
                if isinstance(upload, FileStorage) and upload.filename
            ]
            if not certificates:
                form.certificate.errors.append(
                    "Upload at least one picture of your registration form, ID, or certificate of enrollment."
                )
                return render_template(
                    "register.html",
                    form=form,
                    resubmission=bool(resubmission_account),
                ), 400
            if len(certificates) > MAX_CERTIFICATE_IMAGES:
                form.certificate.errors.append(
                    f"Upload no more than {MAX_CERTIFICATE_IMAGES} pictures."
                )
                return render_template(
                    "register.html",
                    form=form,
                    resubmission=bool(resubmission_account),
                ), 400

            normalized_certificates = []
            for certificate in certificates:
                extension = Path(certificate.filename).suffix.lower()
                if (
                    extension not in ALLOWED_AVATAR_EXTENSIONS
                    or certificate.mimetype
                    != AVATAR_MIMETYPES_BY_EXTENSION[extension]
                ):
                    form.certificate.errors.append(
                        "Upload JPG, PNG, or WEBP pictures only."
                    )
                    return render_template(
                        "register.html",
                        form=form,
                        resubmission=bool(resubmission_account),
                    ), 400

                certificate_data = certificate.stream.read(
                    current_app.config["MAX_CERTIFICATE_BYTES"] + 1
                )
                if len(certificate_data) > current_app.config["MAX_CERTIFICATE_BYTES"]:
                    form.certificate.errors.append(
                        "Each picture must be "
                        f"{format_file_size(current_app.config['MAX_CERTIFICATE_BYTES'])} or smaller."
                    )
                    return render_template(
                        "register.html",
                        form=form,
                        resubmission=bool(resubmission_account),
                    ), 400

                try:
                    normalized_certificates.append(decode_avatar(certificate_data))
                except (
                    Image.DecompressionBombError,
                    Image.DecompressionBombWarning,
                    OSError,
                    ValueError,
                    UnidentifiedImageError,
                ):
                    form.certificate.errors.append(
                        "One of those files isn't a supported, valid image."
                    )
                    return render_template(
                        "register.html",
                        form=form,
                        resubmission=bool(resubmission_account),
                    ), 400

            db = None
            certificate_filenames = [
                f"{uuid.uuid4().hex}.png" for _ in normalized_certificates
            ]
            certificate_folder = Path(
                current_app.config["CERTIFICATE_UPLOAD_FOLDER"]
            )

            try:
                db = get_db()
                existing_user = db.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
                if existing_user and (
                    not resubmission_account
                    or existing_user["id"] != resubmission_account["id"]
                ):
                    form.username.errors.append("That username is already taken.")
                    return render_template(
                        "register.html",
                        form=form,
                        resubmission=bool(resubmission_account),
                    ), 409
                duplicate_student_number = db.execute(
                    """
                    SELECT 1 FROM users
                    WHERE student_number = ? COLLATE NOCASE
                      AND (? IS NULL OR id != ?)
                    """,
                    (
                        student_number,
                        resubmission_account["id"] if resubmission_account else None,
                        resubmission_account["id"] if resubmission_account else None,
                    ),
                ).fetchone()
                if duplicate_student_number:
                    form.student_number.errors.append(
                        "That student number is already registered."
                    )
                    return render_template(
                        "register.html",
                        form=form,
                        resubmission=bool(resubmission_account),
                    ), 409

                for filename, image in zip(
                    certificate_filenames, normalized_certificates
                ):
                    (certificate_folder / filename).write_bytes(image)
                password_hash = generate_password_hash(form.password.data)
                if resubmission_account:
                    cursor = db.execute(
                        """
                        UPDATE users
                        SET student_number = ?, full_name = ?, password_hash = ?,
                            approval_status = 'pending',
                            approval_requested_at = CURRENT_TIMESTAMP,
                            approval_decided_at = NULL,
                            certificate_filename = ?
                        WHERE id = ? AND role = 'student'
                          AND approval_status = 'rejected'
                        """,
                        (
                            student_number,
                            full_name,
                            password_hash,
                            certificate_filenames[0],
                            resubmission_account["id"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        db.rollback()
                        for filename in certificate_filenames:
                            delete_certificate_file(filename)
                        flash(
                            "That registration can no longer be resubmitted.",
                            "error",
                        )
                        return redirect(url_for("register"))
                    db.execute(
                        "DELETE FROM registration_documents WHERE user_id = ?",
                        (resubmission_account["id"],),
                    )
                    user_id = resubmission_account["id"]
                else:
                    cursor = db.execute(
                        """
                        INSERT INTO users
                            (username, student_number, full_name, password_hash,
                             approval_status, approval_requested_at,
                             certificate_filename)
                        VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP, ?)
                        """,
                        (
                            username,
                            student_number,
                            full_name,
                            password_hash,
                            certificate_filenames[0],
                        ),
                    )
                    user_id = cursor.lastrowid
                db.executemany(
                    """
                    INSERT INTO registration_documents (user_id, filename, position)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (user_id, filename, position)
                        for position, filename in enumerate(certificate_filenames)
                    ],
                )
                db.commit()
            except sqlite3.IntegrityError as error:
                if db is not None:
                    db.rollback()
                for filename in certificate_filenames:
                    delete_certificate_file(filename)
                if "users.username" in str(error):
                    form.username.errors.append("That username is already taken.")
                elif (
                    "users.student_number" in str(error)
                    or "users_student_number_unique" in str(error)
                ):
                    form.student_number.errors.append(
                        "That student number is already registered."
                    )
                else:
                    current_app.logger.exception("Registration insert failed.")
                    flash("We couldn't complete registration. Please try again.", "error")
                    return render_template(
                        "register.html",
                        form=form,
                        resubmission=bool(resubmission_account),
                    ), 500
                return render_template(
                    "register.html",
                    form=form,
                    resubmission=bool(resubmission_account),
                ), 409
            except sqlite3.Error:
                if db is not None:
                    db.rollback()
                for filename in certificate_filenames:
                    delete_certificate_file(filename)
                current_app.logger.exception("Database error during registration.")
                flash("We couldn't complete registration. Please try again.", "error")
                return render_template(
                    "register.html",
                    form=form,
                    resubmission=bool(resubmission_account),
                ), 500
            except OSError:
                if db is not None:
                    db.rollback()
                for filename in certificate_filenames:
                    delete_certificate_file(filename)
                current_app.logger.exception("Could not save enrollment certificate.")
                flash("We couldn't complete registration. Please try again.", "error")
                return render_template(
                    "register.html",
                    form=form,
                    resubmission=bool(resubmission_account),
                ), 500

            if resubmission_account:
                flash(
                    "Your registration documents were resubmitted. An administrator "
                    "must review them before marketplace access is restored. You may "
                    "receive an approval or rejection notification within 48 hours.",
                    "success",
                )
            else:
                flash(
                    "Your account request was submitted. An administrator must approve it "
                    "before you can sign in. You may receive an approval or rejection "
                    "notification within 48 hours.",
                    "success",
                )
            return redirect(url_for("login"))

        csrf_field = getattr(form, "csrf_token", None)
        if request.method == "POST" and csrf_field and csrf_field.errors:
            flash("Your request could not be verified. Please reload and try again.", "error")
        return render_template(
            "register.html",
            form=form,
            resubmission=bool(resubmission_account),
        ), (
            400 if request.method == "POST" else 200
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        form = LoginForm()
        if request.method == "POST" and csrf_token_is_valid(form):
            try:
                allowed, retry_after = auth_rate_limit("login", record_attempt=False)
            except sqlite3.Error:
                db = g.get("db")
                if db is not None:
                    db.rollback()
                current_app.logger.exception("Could not check sign-in rate limit.")
                return "Sign-in is temporarily unavailable.", 503
            if not allowed:
                return (
                    "Too many failed sign-in attempts. Please try again later.",
                    429,
                    {"Retry-After": str(retry_after)},
                )
        if form.validate_on_submit():
            identity = form.username_or_email.data.strip()
            try:
                user = get_db().execute(
                    """
                    SELECT id, username, full_name, role, password_hash,
                           approval_status, approval_requested_at, banned_at, ban_reason,
                           CASE
                               WHEN datetime(approval_requested_at, '+48 hours')
                                    <= CURRENT_TIMESTAMP THEN 1
                               ELSE 0
                           END AS review_overdue,
                           EXISTS (
                               SELECT 1 FROM notifications
                               WHERE notifications.recipient_id = users.id
                                 AND notifications.kind = 'account'
                                 AND notifications.read_at IS NULL
                           ) AS has_unread_account_notification
                    FROM users
                    WHERE username = ? OR email = ?
                    """,
                    (identity, identity.lower()),
                ).fetchone()
            except sqlite3.Error:
                current_app.logger.exception("Database error during sign-in.")
                flash("We couldn't sign you in right now. Please try again.", "error")
                return render_template("login.html", form=form), 500
            if user is None or not check_password_hash(
                user["password_hash"], form.password.data
            ):
                try:
                    auth_rate_limit("login", record_attempt=True)
                except sqlite3.Error:
                    db = g.get("db")
                    if db is not None:
                        db.rollback()
                    current_app.logger.exception(
                        "Could not record failed sign-in attempt."
                    )
                    return "Sign-in is temporarily unavailable.", 503
                flash("The username/email or password is incorrect.", "error")
                return render_template("login.html", form=form), 401
            if user["banned_at"] is not None:
                message = "This account has been banned."
                if user["ban_reason"]:
                    message += f" Reason: {user['ban_reason']}"
                flash(message, "error")
                return render_template("login.html", form=form), 403
            if user["approval_status"] == "pending":
                if user["review_overdue"]:
                    flash(
                        "Your account is still under review. Please contact the "
                        "administrator if you need assistance.",
                        "error",
                    )
                else:
                    flash(
                        "Your account is awaiting administrator review. You may "
                        "receive an approval or rejection notification within "
                        "48 hours.",
                        "error",
                    )
                return render_template("login.html", form=form), 403
            if user["approval_status"] == "rejected":
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["full_name"] = user["full_name"]
                session["role"] = user["role"]
                session.permanent = True
                flash(
                    "Your account request was not approved. Review the account "
                    "notification and resubmit your documents.",
                    "error",
                )
                return redirect(url_for("notifications", category="account"))

            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            session.permanent = True
            flash("Welcome back.", "success")
            if user["has_unread_account_notification"]:
                return redirect(url_for("notifications", category="account"))
            next_url = request.form.get("next") or request.args.get("next", "")
            if is_safe_internal_url(next_url):
                return redirect(next_url)
            return redirect(url_for("index"))

        csrf_field = getattr(form, "csrf_token", None)
        if request.method == "POST" and csrf_field and csrf_field.errors:
            flash("Your request could not be verified. Please reload and try again.", "error")
        return render_template("login.html", form=form), (
            400 if request.method == "POST" else 200
        )

    @app.post("/logout")
    def logout():
        form = LogoutForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("index"))
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("login"))

    @app.route("/listings/new", methods=["GET", "POST"])
    @login_required
    def create_listing():
        form = ListingForm()
        if form.validate_on_submit():
            title = form.title.data.strip()
            description = form.description.data.strip()
            listing_type = form.listing_type.data
            if not title:
                form.title.errors.append("Title is required.")
            if not description:
                form.description.errors.append("Description is required.")
            if form.errors:
                return render_template(
                    "listing_form.html",
                    form=form,
                    image_size_limit=listing_image_size_limit_label(),
                ), 400
            listing_stock = form.stock.data if listing_type == "product" else 0

            try:
                price_cents = int(Decimal(form.price.data) * 100)
            except (InvalidOperation, TypeError, ValueError):
                form.price.errors.append("Enter a valid price.")
                return render_template(
                    "listing_form.html",
                    form=form,
                    image_size_limit=listing_image_size_limit_label(),
                ), 400

            image_filename = None
            image = form.image.data
            if image and image.filename:
                extension = Path(image.filename).suffix.lower()
                if (
                    extension not in ALLOWED_AVATAR_EXTENSIONS
                    or image.mimetype != AVATAR_MIMETYPES_BY_EXTENSION[extension]
                ):
                    form.image.errors.append("Upload a JPG, PNG, or WEBP image.")
                    return render_template(
                        "listing_form.html",
                        form=form,
                        image_size_limit=listing_image_size_limit_label(),
                    ), 400
                image_data = image.stream.read(
                    current_app.config["MAX_LISTING_IMAGE_BYTES"] + 1
                )
                if len(image_data) > current_app.config["MAX_LISTING_IMAGE_BYTES"]:
                    form.image.errors.append(
                        f"Images must be {listing_image_size_limit_label()} or smaller."
                    )
                    return render_template(
                        "listing_form.html",
                        form=form,
                        image_size_limit=listing_image_size_limit_label(),
                    ), 400
                try:
                    normalized_image = decode_avatar(image_data)
                except (
                    Image.DecompressionBombError,
                    Image.DecompressionBombWarning,
                    OSError,
                    ValueError,
                    UnidentifiedImageError,
                ):
                    form.image.errors.append("That file isn't a supported, valid image.")
                    return render_template(
                        "listing_form.html",
                        form=form,
                        image_size_limit=listing_image_size_limit_label(),
                    ), 400
                image_filename = f"{uuid.uuid4().hex}.png"
                image_path = Path(current_app.config["LISTING_UPLOAD_FOLDER"]) / image_filename
                try:
                    image_path.write_bytes(normalized_image)
                except OSError:
                    delete_listing_image_file(image_filename)
                    current_app.logger.exception("Could not save listing image.")
                    flash("We couldn't publish your listing. Please try again.", "error")
                    return render_template(
                        "listing_form.html",
                        form=form,
                        image_size_limit=listing_image_size_limit_label(),
                    ), 500

            db = get_db()
            try:
                cursor = db.execute(
                    """
                    INSERT INTO listings
                        (seller_id, listing_type, title, description, price_cents,
                         category, stock, image_filename, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        session["user_id"],
                        listing_type,
                        title,
                        description,
                        price_cents,
                        form.category.data,
                        listing_stock,
                        image_filename,
                    ),
                )
                db.commit()
            except sqlite3.Error:
                db.rollback()
                if image_filename:
                    delete_listing_image_file(image_filename)
                current_app.logger.exception("Database error creating listing.")
                flash("We couldn't publish your listing. Please try again.", "error")
                return render_template(
                    "listing_form.html",
                    form=form,
                    image_size_limit=listing_image_size_limit_label(),
                ), 500

            listing_id = cursor.lastrowid
            flash("Your listing is now published.", "success")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        csrf_field = getattr(form, "csrf_token", None)
        if request.method == "POST" and csrf_field and csrf_field.errors:
            flash("Your request could not be verified. Please reload and try again.", "error")
        return render_template(
            "listing_form.html",
            form=form,
            image_size_limit=listing_image_size_limit_label(),
        ), (400 if request.method == "POST" else 200)

    @app.get("/my-listings")
    @login_required
    def my_listings():
        db = get_db()
        listings = get_db().execute(
            """
            SELECT id, listing_type, title, category, price_cents, stock, status,
                   availability, created_at
            FROM listings
            WHERE seller_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (session["user_id"],),
        ).fetchall()
        incoming_orders = db.execute(
            """
            SELECT o.id, o.quantity, o.total_price_cents, o.payment_method,
                   o.created_at, o.order_type, o.stock_reserved,
                   l.title AS listing_title, l.image_filename,
                   buyer.username AS buyer_username, buyer.full_name AS buyer_name
            FROM orders AS o
            JOIN listings AS l ON l.id = o.listing_id
            JOIN users AS buyer ON buyer.id = o.buyer_id
            WHERE o.seller_id = ? AND o.status = 'pending'
            ORDER BY o.created_at ASC, o.id ASC
            """,
            (session["user_id"],),
        ).fetchall()
        return render_template(
            "my_listings.html",
            listings=listings,
            incoming_orders=incoming_orders,
            restock_form=RestockListingForm(),
            order_status_form=OrderStatusForm(),
            availability_form=ListingAvailabilityForm(),
        )

    @app.post("/listings/<int:listing_id>/availability")
    @login_required
    def update_listing_availability(listing_id):
        form = ListingAvailabilityForm()
        if not form.validate_on_submit():
            flash("Choose a valid availability status.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        db = get_db()
        listing = db.execute(
            "SELECT seller_id, listing_type, status FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        if listing is None:
            abort(404)
        if listing["seller_id"] != session["user_id"]:
            abort(403)
        if listing["listing_type"] != "product" or listing["status"] == "removed":
            flash("Availability can only be changed for product listings that have not been archived.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        try:
            updated = db.execute(
                """
                UPDATE listings SET availability = ?
                WHERE id = ? AND seller_id = ? AND listing_type = 'product'
                  AND status != 'removed'
                """,
                (form.availability.data, listing_id, session["user_id"]),
            )
            if updated.rowcount != 1:
                db.rollback()
                flash("That listing could not be updated.", "error")
                return redirect(url_for("listing_detail", listing_id=listing_id))
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not update product availability.")
            flash("We couldn't update availability. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        flash(
            "Product marked unavailable."
            if form.availability.data == "unavailable"
            else "Product marked available.",
            "success",
        )
        return redirect(url_for("listing_detail", listing_id=listing_id))

    @app.post("/listings/<int:listing_id>/restock")
    @login_required
    def restock_listing(listing_id):
        form = RestockListingForm()
        if not form.validate_on_submit():
            if getattr(form, "csrf_token", None) and form.csrf_token.errors:
                flash("Your request could not be verified. Please reload and try again.", "error")
            else:
                flash("Enter a whole-number restock quantity from 1 to 1,000,000.", "error")
            return redirect(url_for("my_listings"))

        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute(
                """
                SELECT seller_id, listing_type, status, stock, title
                FROM listings WHERE id = ?
                """,
                (listing_id,),
            ).fetchone()
            if listing is None:
                db.rollback()
                abort(404)
            if listing["seller_id"] != session["user_id"]:
                db.rollback()
                abort(403)
            if listing["listing_type"] != "product":
                db.rollback()
                flash("Service listings cannot be restocked.", "error")
                return redirect(url_for("my_listings"))
            if listing["status"] == "removed" or (
                listing["status"] == "sold" and listing["stock"] != 0
            ):
                db.rollback()
                flash("Archived or unavailable listings cannot be restocked.", "error")
                return redirect(url_for("my_listings"))

            available_stock = listing["stock"] + form.quantity.data
            pending_preorders = db.execute(
                """
                SELECT id, buyer_id, quantity
                FROM orders
                WHERE listing_id = ? AND order_type = 'preorder'
                  AND stock_reserved = 0 AND status IN ('pending', 'confirmed')
                ORDER BY created_at, id
                """,
                (listing_id,),
            ).fetchall()
            reserved_order_count = 0
            reserved_unit_count = 0
            for preorder_order in pending_preorders:
                if preorder_order["quantity"] > available_stock:
                    break
                reserved = db.execute(
                    """
                    UPDATE orders SET stock_reserved = 1
                    WHERE id = ? AND stock_reserved = 0
                      AND status IN ('pending', 'confirmed')
                    """,
                    (preorder_order["id"],),
                )
                if reserved.rowcount != 1:
                    raise sqlite3.DatabaseError(
                        "Pre-order reservation changed during restock."
                    )
                available_stock -= preorder_order["quantity"]
                reserved_order_count += 1
                reserved_unit_count += preorder_order["quantity"]
                create_notification(
                    db,
                    preorder_order["buyer_id"],
                    session["user_id"],
                    "order",
                    (
                        f"The seller restocked {listing['title']} and reserved "
                        "your pre-ordered units."
                    ),
                    order_id=preorder_order["id"],
                )

            updated = db.execute(
                """
                UPDATE listings
                SET stock = ?,
                    status = CASE WHEN ? = 0 THEN 'sold' ELSE 'active' END
                WHERE id = ? AND seller_id = ?
                  AND (status = 'active' OR (status = 'sold' AND stock = 0))
                """,
                (
                    available_stock,
                    available_stock,
                    listing_id,
                    session["user_id"],
                ),
            )
            if updated.rowcount != 1:
                db.rollback()
                flash("This listing can no longer be restocked.", "error")
                return redirect(url_for("my_listings"))
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not restock listing.")
            flash("We couldn't restock this listing. Please try again.", "error")
            return redirect(url_for("my_listings"))

        if reserved_order_count:
            message = (
                f"Restocked {listing['title']}. Reserved {reserved_unit_count} "
                f"unit(s) for {reserved_order_count} existing pre-order(s); "
                f"{available_stock} unit(s) remain available."
            )
        else:
            message = f"Added {form.quantity.data} unit(s) to {listing['title']}."
            if pending_preorders:
                message += " No existing pre-orders could be fully covered yet."
        flash(message, "success")
        return redirect(url_for("my_listings"))

    @app.get("/listings/<int:listing_id>")
    def listing_detail(listing_id):
        listing = get_db().execute(
            """
            SELECT l.*, u.username AS seller_username, u.full_name AS seller_name,
                   u.avatar_filename AS seller_avatar,
                   u.approval_status AS seller_approval_status,
                   u.banned_at AS seller_banned_at,
                   (SELECT AVG(r.rating)
                    FROM reviews AS r
                    JOIN orders AS o ON o.id = r.order_id
                    WHERE r.seller_id = l.seller_id
                      AND o.seller_id = l.seller_id
                      AND o.status = 'completed')
                       AS average_rating,
                   (SELECT COUNT(*)
                    FROM reviews AS r
                    JOIN orders AS o ON o.id = r.order_id
                    WHERE r.seller_id = l.seller_id
                      AND o.seller_id = l.seller_id
                      AND o.status = 'completed')
                       AS rating_count,
                   (SELECT AVG(r.rating)
                    FROM reviews AS r JOIN orders AS o ON o.id = r.order_id
                    WHERE o.listing_id = l.id) AS listing_average_rating,
                   (SELECT COUNT(*)
                    FROM reviews AS r JOIN orders AS o ON o.id = r.order_id
                    WHERE o.listing_id = l.id) AS listing_rating_count,
                   (SELECT COALESCE(SUM(o.quantity), 0)
                    FROM orders AS o
                    WHERE o.listing_id = l.id AND o.status = 'completed')
                       AS listing_sold_count
            FROM listings AS l
            JOIN users AS u ON u.id = l.seller_id
            WHERE l.id = ? AND l.status != 'removed'
            """,
            (listing_id,),
        ).fetchone()
        if listing is None:
            abort(404)
        order_form = PlaceOrderForm()
        report_form = ReportForm()
        archive_form = ArchiveListingForm()
        availability_form = ListingAvailabilityForm()
        availability_form.availability.data = listing["availability"]
        available = (
            listing["status"] == "active"
            if listing["listing_type"] == "service"
            else listing["availability"] == "available"
            and (
                listing["status"] == "active"
                or (listing["status"] == "sold" and listing["stock"] == 0)
            )
        )
        preorder = available and (
            listing["stock"] <= PREORDER_STOCK_THRESHOLD
        )
        is_seller = session.get("user_id") == listing["seller_id"]
        reviews = get_db().execute(
            """
            SELECT r.rating, r.feedback, r.created_at, buyer.full_name AS buyer_name
            FROM reviews AS r
            JOIN orders AS o ON o.id = r.order_id
            JOIN users AS buyer ON buyer.id = r.buyer_id
            WHERE o.listing_id = ?
            ORDER BY r.created_at DESC, r.id DESC LIMIT 5
            """,
            (listing_id,),
        ).fetchall()
        return render_template(
            "listing_detail.html",
            listing=listing,
            available=available,
            seller_available=(
                listing["seller_approval_status"] == "approved"
                and listing["seller_banned_at"] is None
            ),
            preorder=preorder,
            preorder_threshold=PREORDER_STOCK_THRESHOLD,
            is_seller=is_seller,
            order_form=order_form,
            report_form=report_form,
            archive_form=archive_form,
            availability_form=availability_form,
            reviews=reviews,
        )

    @app.get("/listings/<int:listing_id>/checkout")
    @login_required
    def order_checkout(listing_id):
        listing = get_db().execute(
            """
            SELECT l.*, u.username AS seller_username, u.full_name AS seller_name
            FROM listings AS l
            JOIN users AS u ON u.id = l.seller_id
            WHERE l.id = ? AND l.status != 'removed'
            """,
            (listing_id,),
        ).fetchone()
        if listing is None:
            abort(404)
        if listing["listing_type"] != "product":
            flash("Service listings are contact-only and cannot be ordered.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        if listing["seller_id"] == session["user_id"]:
            flash("You can't order your own listing.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        available = listing["availability"] == "available" and (
            listing["status"] == "active"
            or (listing["status"] == "sold" and listing["stock"] == 0)
        )
        if not available:
            flash("This listing is no longer available to order.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        raw_quantity = request.args.get("quantity", "1")
        if not re.fullmatch(r"[0-9]+", raw_quantity):
            flash("Enter a valid order quantity.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        try:
            quantity = int(raw_quantity)
        except ValueError:
            flash("Enter a valid order quantity.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        if quantity < 1 or (listing["stock"] > 0 and quantity > listing["stock"]):
            flash("This listing no longer has enough availability.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        preorder = listing["stock"] <= PREORDER_STOCK_THRESHOLD
        form = PlaceOrderForm()
        form.quantity.data = quantity
        form.request_token.data = uuid.uuid4().hex
        return render_template(
            "order_checkout.html",
            listing=listing,
            quantity=quantity,
            total_cents=listing["price_cents"] * quantity,
            preorder=preorder,
            form=form,
        )

    @app.post("/listings/<int:listing_id>/contact")
    @login_required
    def contact_service_provider(listing_id):
        form = ContactProviderForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please reload and try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        db = get_db()
        customer_id = session["user_id"]
        try:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute(
                """
                SELECT l.id, l.seller_id, l.listing_type, l.status,
                       u.approval_status AS seller_approval_status,
                       u.banned_at AS seller_banned_at
                FROM listings AS l
                JOIN users AS u ON u.id = l.seller_id
                WHERE l.id = ? AND l.status != 'removed'
                """,
                (listing_id,),
            ).fetchone()
            if listing is None:
                db.rollback()
                abort(404)
            if listing["seller_id"] == customer_id:
                db.rollback()
                abort(403)
            if listing["listing_type"] == "service" and (
                listing["status"] != "active"
                or listing["seller_approval_status"] != "approved"
                or listing["seller_banned_at"] is not None
            ):
                db.rollback()
                flash(
                    "This service or seller is currently unavailable for new messages.",
                    "error",
                )
                return redirect(url_for("listing_detail", listing_id=listing_id))

            conversation = db.execute(
                """
                SELECT id FROM conversations
                WHERE listing_id = ? AND customer_id = ? AND provider_id = ?
                """,
                (listing_id, customer_id, listing["seller_id"]),
            ).fetchone()
            if conversation is None:
                cursor = db.execute(
                    """
                    INSERT INTO conversations (listing_id, customer_id, provider_id)
                    VALUES (?, ?, ?)
                    """,
                    (listing_id, customer_id, listing["seller_id"]),
                )
                conversation_id = cursor.lastrowid
            else:
                conversation_id = conversation["id"]
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not start listing conversation.")
            flash("We couldn't open this conversation. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        return redirect(url_for("message_conversation", conversation_id=conversation_id))

    @app.get("/messages")
    @login_required
    def inbox():
        user_id = session["user_id"]
        conversations = get_db().execute(
            """
            SELECT c.id, c.created_at, l.id AS listing_id, l.title AS listing_title,
                   CASE WHEN c.customer_id = ? THEN provider.full_name
                        ELSE customer.full_name END AS other_name,
                   CASE WHEN c.customer_id = ? THEN provider.username
                        ELSE customer.username END AS other_username,
                   (SELECT m.body FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS latest_message,
                   (SELECT m.created_at FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS latest_at
            FROM conversations c
            JOIN listings l ON l.id = c.listing_id
            JOIN users customer ON customer.id = c.customer_id
            JOIN users provider ON provider.id = c.provider_id
            WHERE c.customer_id = ? OR c.provider_id = ?
            ORDER BY COALESCE(latest_at, c.created_at) DESC, c.id DESC
            """,
            (user_id, user_id, user_id, user_id),
        ).fetchall()
        return render_template("inbox.html", conversations=conversations)

    @app.get("/notifications")
    @login_required
    def notifications():
        db = get_db()
        user_id = session["user_id"]
        category = request.args.get("category", "all")
        category_kinds = {
            "orders": "order",
            "messages": "message",
            "account": "account",
        }
        if category != "all" and category not in category_kinds:
            abort(400)
        raw_page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)

        category_counts = {"all": 0, "orders": 0, "messages": 0, "account": 0}
        unread_count = 0
        for count_row in db.execute(
            """
            SELECT kind, COUNT(*) AS total_count,
                   SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread_count
            FROM notifications
            WHERE recipient_id = ?
            GROUP BY kind
            """,
            (user_id,),
        ).fetchall():
            count_category = {
                "order": "orders",
                "message": "messages",
                "account": "account",
            }.get(count_row["kind"])
            if count_category:
                category_counts[count_category] = count_row["total_count"]
                category_counts["all"] += count_row["total_count"]
                unread_count += count_row["unread_count"]

        total_count = category_counts[category]
        total_pages = max(
            1, (total_count + NOTIFICATION_PAGE_SIZE - 1) // NOTIFICATION_PAGE_SIZE
        )
        if page > total_pages:
            return redirect(
                url_for("notifications", category=category, page=total_pages)
            )

        kind_filter = ""
        parameters = [user_id]
        if category != "all":
            kind_filter = "AND n.kind = ?"
            parameters.append(category_kinds[category])
        parameters.extend(
            [NOTIFICATION_PAGE_SIZE, (page - 1) * NOTIFICATION_PAGE_SIZE]
        )
        items = db.execute(
            f"""
            SELECT n.*, actor.full_name AS actor_name, l.title AS listing_title,
                   recipient.approval_status AS recipient_approval_status,
                   o.buyer_id AS order_buyer_id, o.seller_id AS order_seller_id,
                   c.id AS existing_conversation_id,
                   c.customer_id AS conversation_customer_id,
                   c.provider_id AS conversation_provider_id
            FROM notifications n
            LEFT JOIN users actor ON actor.id = n.actor_id
            JOIN users recipient ON recipient.id = n.recipient_id
            LEFT JOIN orders o ON o.id = n.order_id
            LEFT JOIN listings l ON l.id = o.listing_id
            LEFT JOIN conversations c ON c.id = n.conversation_id
            WHERE n.recipient_id = ?
            {kind_filter}
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
        notifications = []
        for item in items:
            notification = dict(item)
            if notification["kind"] == "order" and notification["listing_title"]:
                order_reference = f"#{notification['order_id']}"
                notification["display_body"] = notification["body"].replace(
                    f"Receipt for transaction {order_reference} was confirmed.",
                    f"Receipt for {notification['listing_title']} is ready.",
                ).replace(
                    f"Transaction {order_reference}",
                    notification["listing_title"],
                ).replace(
                    f"transaction {order_reference}",
                    notification["listing_title"],
                )
            else:
                notification["display_body"] = notification["body"]
            notification["destination_url"] = None
            notification["destination_label"] = "Unavailable"
            notification["icon"] = "account"
            notification["decision_status"] = None
            if notification["kind"] == "message":
                notification["icon"] = "message"
                conversation = notification["existing_conversation_id"]
                if (
                    conversation is not None
                    and user_id
                    in (
                        notification["conversation_customer_id"],
                        notification["conversation_provider_id"],
                    )
                ):
                    notification["destination_url"] = url_for(
                        "message_conversation", conversation_id=conversation
                    )
                    notification["destination_label"] = "View conversation"
            elif notification["kind"] == "order":
                notification["icon"] = (
                    "receipt"
                    if notification["display_body"].startswith("Receipt for ")
                    else "order"
                )
                order_id = notification["order_id"]
                if (
                    order_id is not None
                    and notification["listing_title"] is not None
                    and user_id
                    in (
                        notification["order_buyer_id"],
                        notification["order_seller_id"],
                    )
                ):
                    notification["destination_url"] = url_for(
                        "order_detail",
                        order_id=order_id,
                        _anchor=(
                            "transaction-information-title"
                            if notification["icon"] == "receipt"
                            else None
                        ),
                    )
                    notification["destination_label"] = (
                        "View receipt"
                        if notification["icon"] == "receipt"
                        else "View transaction"
                    )
            elif notification["kind"] == "account":
                if notification["body"].startswith("APPROVED:"):
                    notification["decision_status"] = "approved"
                elif notification["body"].startswith("REJECTED:"):
                    notification["decision_status"] = "rejected"
                rejected = notification["recipient_approval_status"] == "rejected"
                notification["destination_url"] = url_for(
                    "register" if rejected else "profile"
                )
                notification["destination_label"] = (
                    "Resubmit documents" if rejected else "View account"
                )

            created_at = notification["created_at"]
            try:
                parsed_created_at = datetime.fromisoformat(created_at)
            except (TypeError, ValueError):
                notification["created_label"] = "Time unavailable"
                notification["created_iso"] = None
            else:
                notification["created_label"] = format_datetime(created_at)
                notification["created_iso"] = parsed_created_at.isoformat()
            notifications.append(notification)
        mark_all_form = NotificationActionForm()
        return render_template(
            "notifications.html",
            notifications=notifications,
            category=category,
            category_counts=category_counts,
            unread_count=unread_count,
            total_count=total_count,
            page=page,
            total_pages=total_pages,
            mark_all_form=mark_all_form,
        )

    @app.post("/notifications/read-all")
    @login_required
    def mark_all_notifications_read():
        form = NotificationActionForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please reload and try again.", "error")
            return redirect(url_for("notifications"))
        cursor = get_db().execute(
            """
            UPDATE notifications
            SET read_at = CURRENT_TIMESTAMP
            WHERE recipient_id = ? AND read_at IS NULL
            """,
            (session["user_id"],),
        )
        get_db().commit()
        if cursor.rowcount:
            flash(f"Marked {cursor.rowcount} notification(s) as read.", "success")
        else:
            flash("There are no unread notifications to mark.", "success")
        return redirect(url_for("notifications"))

    @app.post("/notifications/<int:notification_id>/open")
    @login_required
    def open_notification(notification_id):
        form = NotificationActionForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please reload and try again.", "error")
            return redirect(url_for("notifications"))

        db = get_db()
        user_id = session["user_id"]
        notification = db.execute(
            """
            SELECT id, kind, body, conversation_id, order_id
            FROM notifications
            WHERE id = ? AND recipient_id = ?
            """,
            (notification_id, user_id),
        ).fetchone()
        if notification is None:
            abort(404)

        destination = None
        if notification["kind"] == "message" and notification["conversation_id"]:
            conversation = db.execute(
                """
                SELECT c.id
                FROM conversations c
                JOIN listings l ON l.id = c.listing_id
                JOIN users customer ON customer.id = c.customer_id
                JOIN users provider ON provider.id = c.provider_id
                WHERE c.id = ? AND (c.customer_id = ? OR c.provider_id = ?)
                """,
                (notification["conversation_id"], user_id, user_id),
            ).fetchone()
            if conversation:
                destination = url_for(
                    "message_conversation",
                    conversation_id=notification["conversation_id"],
                )
        elif notification["kind"] == "order" and notification["order_id"]:
            order = db.execute(
                """
                SELECT o.id
                FROM orders o
                JOIN listings l ON l.id = o.listing_id
                JOIN users buyer ON buyer.id = o.buyer_id
                JOIN users seller ON seller.id = o.seller_id
                WHERE o.id = ? AND (o.buyer_id = ? OR o.seller_id = ?)
                """,
                (notification["order_id"], user_id, user_id),
            ).fetchone()
            if order:
                destination = url_for(
                    "order_detail",
                    order_id=order["id"],
                    _anchor=(
                        "transaction-information-title"
                        if notification["body"].startswith("Receipt for ")
                        else None
                    ),
                )
        elif notification["kind"] == "account":
            recipient = db.execute(
                "SELECT approval_status FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            destination = url_for(
                "register"
                if recipient and recipient["approval_status"] == "rejected"
                else "profile"
            )

        db.execute(
            """
            UPDATE notifications
            SET read_at = COALESCE(read_at, CURRENT_TIMESTAMP)
            WHERE id = ? AND recipient_id = ?
            """,
            (notification_id, user_id),
        )
        db.commit()
        if destination:
            return redirect(destination)
        flash("The item linked to this notification is no longer available.", "error")
        return redirect(url_for("notifications"))

    @app.route("/messages/<int:conversation_id>", methods=["GET", "POST"])
    @login_required
    def message_conversation(conversation_id):
        user_id = session["user_id"]
        db = get_db()
        conversation = db.execute(
            """
            SELECT c.*, l.title AS listing_title, l.listing_type,
                   l.price_cents AS listing_price_cents,
                   l.image_filename AS listing_image_filename,
                   l.status AS listing_status,
                   l.stock AS listing_stock,
                   customer.full_name AS customer_name,
                   customer.username AS customer_username,
                   provider.full_name AS provider_name,
                   provider.username AS provider_username
            FROM conversations c
            LEFT JOIN listings l ON l.id = c.listing_id
            JOIN users customer ON customer.id = c.customer_id
            JOIN users provider ON provider.id = c.provider_id
            WHERE c.id = ? AND (c.customer_id = ? OR c.provider_id = ?)
            """,
            (conversation_id, user_id, user_id),
        ).fetchone()
        if conversation is None:
            abort(404)
        conversation = dict(conversation)
        conversation["listing_available"] = (
            conversation["listing_title"] is not None
            and (
                conversation["listing_status"] == "active"
                or (
                    conversation["listing_status"] == "sold"
                    and conversation["listing_stock"] == 0
                )
            )
        )
        if not conversation["listing_available"]:
            if conversation["listing_title"] is None:
                conversation["listing_title"] = "Listing unavailable"

        if request.method == "GET":
            db.execute(
                """
                UPDATE notifications
                SET read_at = CURRENT_TIMESTAMP
                WHERE recipient_id = ? AND conversation_id = ? AND read_at IS NULL
                """,
                (user_id, conversation_id),
            )
            db.commit()

        form = MessageForm()

        def render_conversation(status_code=200):
            rows = db.execute(
                """
                SELECT m.*, u.full_name AS sender_name, u.username AS sender_username
                FROM messages m JOIN users u ON u.id = m.sender_id
                WHERE m.conversation_id = ?
                ORDER BY m.id
                """,
                (conversation_id,),
            ).fetchall()
            messages = []
            previous_day = None
            for row in rows:
                message = dict(row)
                try:
                    created = datetime.fromisoformat(message["created_at"])
                except (TypeError, ValueError):
                    message["day_key"] = None
                    message["day_label"] = "Date unavailable"
                    message["time_label"] = message["created_at"] or "Time unavailable"
                else:
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    created = created.astimezone(PHILIPPINE_TIMEZONE)
                    message["day_key"] = created.date().isoformat()
                    message["day_label"] = created.strftime("%B %d, %Y")
                    message["time_label"] = created.strftime("%I:%M %p")
                message["show_day_separator"] = message["day_key"] != previous_day
                message["grouped_after_previous"] = (
                    not message["show_day_separator"]
                    and bool(messages)
                    and messages[-1]["sender_id"] == message["sender_id"]
                )
                messages.append(message)
                previous_day = message["day_key"]
            return render_template(
                "conversation.html",
                conversation=conversation,
                messages=messages,
                form=form,
                listing_image_is_available=listing_image_is_available,
            ), status_code

        if request.method == "POST":
            if not form.validate_on_submit():
                csrf_field = getattr(form, "csrf_token", None)
                if csrf_field and csrf_field.errors:
                    flash("Your request could not be verified. Please reload and try again.", "error")
                return render_conversation(400)
            body = (form.body.data or "").strip()
            if not body or len(body) > 2000:
                form.body.errors.append("Enter a message up to 2,000 characters.")
            else:
                try:
                    db.execute(
                        """
                        INSERT INTO messages (conversation_id, sender_id, body)
                        VALUES (?, ?, ?)
                        """,
                        (conversation_id, user_id, body),
                    )
                    recipient_id = (
                        conversation["provider_id"]
                        if user_id == conversation["customer_id"]
                        else conversation["customer_id"]
                    )
                    create_notification(
                        db,
                        recipient_id,
                        user_id,
                        "message",
                        f"New message from {conversation['customer_name'] if user_id == conversation['customer_id'] else conversation['provider_name']} about {conversation['listing_title']}.",
                        conversation_id=conversation_id,
                    )
                    db.commit()
                except sqlite3.Error:
                    db.rollback()
                    current_app.logger.exception(
                        "Could not save conversation message or notification."
                    )
                    flash("We couldn't send your message. Your draft is still here; please try again.", "error")
                    form.body.data = body
                    return render_conversation(503)
                flash("Your message was sent.", "success")
                return redirect(
                    url_for("message_conversation", conversation_id=conversation_id)
                )
            return render_conversation(400)

        return render_conversation()

    def render_support_conversation(conversation_id, *, admin_view):
        db = get_db()
        parameters = [conversation_id]
        access_clause = ""
        if not admin_view:
            access_clause = " AND c.user_id = ?"
            parameters.append(session["user_id"])
        conversation = db.execute(
            """
            SELECT c.*, u.full_name AS user_name, u.username AS user_username,
                   u.avatar_filename AS user_avatar_filename
            FROM support_conversations c
            JOIN users u ON u.id = c.user_id
            WHERE c.id = ?
            """ + access_clause,
            parameters,
        ).fetchone()
        if conversation is None:
            abort(404)

        if request.method == "GET":
            read_column = (
                "admin_last_read_message_id"
                if admin_view
                else "user_last_read_message_id"
            )
            db.execute(
                f"""
                UPDATE support_conversations
                SET {read_column} = COALESCE(
                    (SELECT MAX(id) FROM support_messages
                     WHERE conversation_id = ?),
                    {read_column}
                )
                WHERE id = ?
                """,
                (conversation_id, conversation_id),
            )
            db.commit()

        form = MessageForm()
        resolve_form = SupportConversationActionForm()
        if request.method == "POST":
            if request.form.get("action") == "resolve":
                if not admin_view:
                    abort(403)
                if not resolve_form.validate_on_submit():
                    flash(
                        "Your request could not be verified. Please reload and try again.",
                        "error",
                    )
                    return redirect(
                        url_for(
                            "admin_support_conversation",
                            conversation_id=conversation_id,
                        )
                    )
                try:
                    cursor = db.execute(
                        """
                        UPDATE support_conversations
                        SET status = 'resolved', resolved_by = ?,
                            resolved_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status = 'open'
                        """,
                        (session["user_id"], conversation_id),
                    )
                    db.commit()
                except sqlite3.Error:
                    db.rollback()
                    current_app.logger.exception(
                        "Could not resolve support conversation."
                    )
                    flash("We couldn't resolve this conversation. Please try again.", "error")
                else:
                    flash(
                        "Support conversation marked as resolved."
                        if cursor.rowcount
                        else "This support conversation is already resolved.",
                        "success" if cursor.rowcount else "info",
                    )
                return redirect(
                    url_for(
                        "admin_support_conversation",
                        conversation_id=conversation_id,
                    )
                )

            if not form.validate_on_submit():
                csrf_field = getattr(form, "csrf_token", None)
                if csrf_field and csrf_field.errors:
                    flash(
                        "Your request could not be verified. Please reload and try again.",
                        "error",
                    )
                messages = support_messages_for(conversation_id)
                return render_template(
                    "support_conversation.html",
                    conversation=conversation,
                    messages=messages,
                    form=form,
                    resolve_form=resolve_form,
                    admin_view=admin_view,
                    avatar_is_available=avatar_is_available,
                ), 400

            body = form.body.data.strip()
            if not body or len(body) > 2000:
                form.body.errors.append("Enter a message up to 2,000 characters.")
            else:
                try:
                    db.execute(
                        """
                        INSERT INTO support_messages
                            (conversation_id, sender_id, body)
                        VALUES (?, ?, ?)
                        """,
                        (conversation_id, session["user_id"], body),
                    )
                    db.execute(
                        """
                        UPDATE support_conversations
                        SET status = 'open', resolved_by = NULL, resolved_at = NULL
                        WHERE id = ?
                        """,
                        (conversation_id,),
                    )
                    db.commit()
                except sqlite3.Error:
                    db.rollback()
                    current_app.logger.exception(
                        "Could not save support conversation message."
                    )
                    flash("We couldn't send your message. Please try again.", "error")
                    form.body.data = body
                    return render_template(
                        "support_conversation.html",
                        conversation=conversation,
                        messages=support_messages_for(conversation_id),
                        form=form,
                        resolve_form=resolve_form,
                        admin_view=admin_view,
                        avatar_is_available=avatar_is_available,
                    ), 503
                flash("Your message was sent.", "success")
                return redirect(
                    url_for(
                        "admin_support_conversation",
                        conversation_id=conversation_id,
                    )
                    if admin_view
                    else url_for("support_chat")
                )

            messages = support_messages_for(conversation_id)
            return render_template(
                "support_conversation.html",
                conversation=conversation,
                messages=messages,
                form=form,
                resolve_form=resolve_form,
                admin_view=admin_view,
                avatar_is_available=avatar_is_available,
            ), 400

        messages = support_messages_for(conversation_id)
        return render_template(
            "support_conversation.html",
            conversation=conversation,
            messages=messages,
            form=form,
            resolve_form=resolve_form,
            admin_view=admin_view,
            avatar_is_available=avatar_is_available,
        )

    def support_messages_for(conversation_id):
        rows = get_db().execute(
            """
            SELECT m.*, u.full_name AS sender_name, u.username AS sender_username,
                   u.role AS sender_role
            FROM support_messages m
            JOIN users u ON u.id = m.sender_id
            WHERE m.conversation_id = ?
            ORDER BY m.id
            """,
            (conversation_id,),
        ).fetchall()
        messages = []
        previous_day = None
        for row in rows:
            message = dict(row)
            try:
                created = datetime.fromisoformat(message["created_at"])
            except (TypeError, ValueError):
                message["day_key"] = None
                message["day_label"] = "Date unavailable"
                message["time_label"] = message["created_at"] or "Time unavailable"
            else:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                created = created.astimezone(PHILIPPINE_TIMEZONE)
                message["day_key"] = created.date().isoformat()
                message["day_label"] = created.strftime("%B %d, %Y")
                message["time_label"] = created.strftime("%b %d, %I:%M %p").replace(
                    " 0", " "
                )
            message["show_day_separator"] = message["day_key"] != previous_day
            message["grouped_after_previous"] = (
                not message["show_day_separator"]
                and bool(messages)
                and messages[-1]["sender_id"] == message["sender_id"]
            )
            messages.append(message)
            previous_day = message["day_key"]
        return messages

    @app.route("/support", methods=["GET", "POST"])
    @login_required
    def support_chat():
        db = get_db()
        try:
            user = db.execute(
                "SELECT role FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
            if user is None:
                session.clear()
                return redirect(url_for("login"))
            if user["role"] == "admin":
                return redirect(url_for("admin_support_inbox"))
            db.execute(
                """
                INSERT INTO support_conversations (user_id)
                VALUES (?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (session["user_id"],),
            )
            conversation_id = db.execute(
                "SELECT id FROM support_conversations WHERE user_id = ?",
                (session["user_id"],),
            ).fetchone()["id"]
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not open support conversation.")
            flash("We couldn't open support chat. Please try again.", "error")
            return redirect(url_for("index"))
        return render_support_conversation(conversation_id, admin_view=False)

    @app.get("/admin/support")
    @admin_required
    def admin_support_inbox():
        conversations = get_db().execute(
            """
            SELECT c.id, c.created_at, u.full_name AS user_name,
                   u.username AS user_username,
                   (SELECT m.body FROM support_messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS latest_message,
                   (SELECT m.created_at FROM support_messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS latest_at,
                   (SELECT COUNT(*) FROM support_messages m
                    WHERE m.conversation_id = c.id
                     AND m.sender_id = c.user_id
                      AND m.id > c.admin_last_read_message_id)
                       AS unread_count
            FROM support_conversations c
            JOIN users u ON u.id = c.user_id
            ORDER BY COALESCE(latest_at, c.created_at) DESC, c.id DESC
            """
        ).fetchall()
        return render_template(
            "admin/support.html", conversations=conversations
        )

    @app.route("/admin/support/<int:conversation_id>", methods=["GET", "POST"])
    @admin_required
    def admin_support_conversation(conversation_id):
        return render_support_conversation(conversation_id, admin_view=True)

    @app.post("/listings/<int:listing_id>/order")
    @login_required
    def place_order(listing_id):
        form = PlaceOrderForm()
        if not form.validate_on_submit():
            if getattr(form, "csrf_token", None) and form.csrf_token.errors:
                flash("Your request could not be verified. Please reload and try again.", "error")
            elif form.payment_method.errors:
                flash("Choose Cash, GCash, or Maya.", "error")
            else:
                flash("Enter a valid order quantity.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        token = form.request_token.data.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            flash("This order form expired. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT id, order_type FROM orders WHERE buyer_id = ? AND request_token = ?",
                (session["user_id"], token),
            ).fetchone()
            if duplicate:
                db.commit()
                flash(
                    "This pre-order request was already placed."
                    if duplicate["order_type"] == "preorder"
                    else "This order was already placed.",
                    "success",
                )
                return redirect(url_for("order_detail", order_id=duplicate["id"]))

            listing = db.execute(
                """
                SELECT id, seller_id, listing_type, title, price_cents, stock,
                       status, availability
                FROM listings WHERE id = ?
                """,
                (listing_id,),
            ).fetchone()
            if listing is None or listing["status"] == "removed":
                db.rollback()
                abort(404)
            if listing["listing_type"] != "product":
                db.rollback()
                flash("Service listings are contact-only and cannot be ordered.", "error")
                return redirect(url_for("listing_detail", listing_id=listing_id))
            if listing["seller_id"] == session["user_id"]:
                db.rollback()
                flash("You can't order your own listing.", "error")
                return redirect(url_for("listing_detail", listing_id=listing_id))
            quantity = form.quantity.data
            orderable = listing["availability"] == "available" and (
                listing["status"] == "active"
                or (listing["status"] == "sold" and listing["stock"] == 0)
            )
            if (
                not orderable
                or (listing["stock"] > 0 and quantity > listing["stock"])
            ):
                db.rollback()
                flash("This listing no longer has enough availability.", "error")
                return redirect(url_for("listing_detail", listing_id=listing_id))

            order_type = (
                "preorder"
                if listing["stock"] <= PREORDER_STOCK_THRESHOLD
                else "purchase"
            )
            stock_reserved = int(listing["stock"] > 0)
            total_cents = listing["price_cents"] * quantity
            if stock_reserved:
                update = db.execute(
                    """
                    UPDATE listings
                    SET stock = stock - ?,
                        status = CASE WHEN stock - ? = 0 THEN 'sold' ELSE status END
                    WHERE id = ? AND status = 'active' AND stock >= ?
                      AND availability = 'available'
                    """,
                    (quantity, quantity, listing_id, quantity),
                )
                if update.rowcount != 1:
                    db.rollback()
                    flash("This listing no longer has enough availability.", "error")
                    return redirect(url_for("listing_detail", listing_id=listing_id))
            elif listing["status"] == "active":
                db.execute(
                    """
                    UPDATE listings SET status = 'sold'
                    WHERE id = ? AND status = 'active' AND stock = 0
                    """,
                    (listing_id,),
                )
            cursor = db.execute(
                """
                INSERT INTO orders
                    (buyer_id, seller_id, listing_id, quantity, unit_price_cents,
                     total_price_cents, status, order_type, stock_reserved,
                     payment_method, request_token)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    session["user_id"],
                    listing["seller_id"],
                    listing_id,
                    quantity,
                    listing["price_cents"],
                    total_cents,
                    order_type,
                    stock_reserved,
                    form.payment_method.data,
                    token,
                ),
            )
            order_id = cursor.lastrowid
            create_notification(
                db,
                listing["seller_id"],
                session["user_id"],
                "order",
                (
                    f"A buyer placed a pre-order request for {listing['title']}."
                    if order_type == "preorder"
                    else f"A buyer placed an order for {listing['title']}."
                ),
                order_id=order_id,
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            current_app.logger.exception("Order or seller notification could not be recorded.")
            flash("We couldn't place your order. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Database error placing order or notification.")
            flash("We couldn't place your order. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))

        if order_type == "preorder":
            flash(
                "Your pre-order request was placed. The seller has been notified; "
                "fulfillment may take longer.",
                "success",
            )
        else:
            flash("Your order was placed and is pending seller confirmation.", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.get("/orders/<int:order_id>")
    @login_required
    def order_detail(order_id):
        user = get_db().execute(
            "SELECT role FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        is_admin = user is not None and user["role"] == "admin"
        order = get_db().execute(
            """
            SELECT o.*, l.title AS listing_title, l.listing_type, l.image_filename,
                   l.status AS listing_status, buyer.username AS buyer_username,
                   buyer.full_name AS buyer_name, seller.username AS seller_username,
                   seller.full_name AS seller_name
            FROM orders AS o
            JOIN listings AS l ON l.id = o.listing_id
            JOIN users AS buyer ON buyer.id = o.buyer_id
            JOIN users AS seller ON seller.id = o.seller_id
            WHERE o.id = ? AND ((o.buyer_id = ? OR o.seller_id = ?) OR ?)
            """,
            (order_id, session["user_id"], session["user_id"], is_admin),
        ).fetchone()
        if order is None:
            abort(404)
        progress_status = order["status"]
        cancelled_by_seller = False
        latest_status_event = get_db().execute(
            """
            SELECT old_status, actor_id, new_status, created_at
            FROM order_events WHERE order_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        completed_by_seller = (
            order["status"] == "completed"
            and latest_status_event is not None
            and latest_status_event["new_status"] == "completed"
            and latest_status_event["actor_id"] == order["seller_id"]
        )
        if progress_status == "cancelled":
            cancelled_by_seller = (
                latest_status_event is not None
                and latest_status_event["new_status"] == "cancelled"
                and latest_status_event["actor_id"] == order["seller_id"]
            )
        else:
            progress_status = order["status"]
        allowed = available_order_actions(
            order, session["user_id"], is_admin=is_admin
        )
        status_form = OrderStatusForm()
        status_form.status.choices = allowed
        review_form = ReviewForm()
        review = get_db().execute(
            "SELECT rating, feedback, created_at FROM reviews WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return render_template(
            "order_detail.html",
            order=order,
            status_form=status_form,
            order_actions=allowed,
            order_progress_statuses=ORDER_PROGRESS_STATUSES,
            progress_status=progress_status,
            latest_status_at=(
                latest_status_event["created_at"]
                if latest_status_event
                else order["created_at"]
            ),
            is_buyer=session["user_id"] == order["buyer_id"],
            is_seller=session["user_id"] == order["seller_id"],
            review_form=review_form,
            review=review,
            cancelled_by_seller=cancelled_by_seller,
            completed_by_seller=completed_by_seller,
            can_review=(
                session["user_id"] == order["buyer_id"]
                and order["status"] == "completed"
                and review is None
            ),
        )

    @app.get("/orders/<int:order_id>/message")
    @login_required
    def order_conversation(order_id):
        db = get_db()
        order = db.execute(
            """
            SELECT id, listing_id, buyer_id, seller_id
            FROM orders WHERE id = ? AND (buyer_id = ? OR seller_id = ?)
            """,
            (order_id, session["user_id"], session["user_id"]),
        ).fetchone()
        if order is None:
            abort(404)
        try:
            db.execute("BEGIN IMMEDIATE")
            conversation = db.execute(
                """
                SELECT id FROM conversations
                WHERE listing_id = ? AND customer_id = ? AND provider_id = ?
                """,
                (order["listing_id"], order["buyer_id"], order["seller_id"]),
            ).fetchone()
            if conversation is None:
                cursor = db.execute(
                    """
                    INSERT INTO conversations (listing_id, customer_id, provider_id)
                    VALUES (?, ?, ?)
                    """,
                    (order["listing_id"], order["buyer_id"], order["seller_id"]),
                )
                conversation_id = cursor.lastrowid
            else:
                conversation_id = conversation["id"]
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception(
                "Could not open order conversation for transaction %s.", order_id
            )
            flash("We couldn't open this conversation. Please try again.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        return redirect(url_for("message_conversation", conversation_id=conversation_id))

    @app.get("/transactions")
    @login_required
    def transactions():
        view = request.args.get("view", "all")
        status = request.args.get("status", "")
        if view not in {"all", "buyer", "seller"}:
            view = "all"
        if status not in ORDER_STATUSES:
            status = ""
        clauses = ["(o.buyer_id = ? OR o.seller_id = ?)"]
        params = [session["user_id"], session["user_id"]]
        if view == "buyer":
            clauses.append("o.buyer_id = ?")
            params.append(session["user_id"])
        elif view == "seller":
            clauses.append("o.seller_id = ?")
            params.append(session["user_id"])
        if status:
            clauses.append("o.status = ?")
            params.append(status)
        db = get_db()
        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_orders = db.execute(
            """
            SELECT COUNT(*)
            FROM orders o JOIN listings l ON l.id = o.listing_id
            JOIN users buyer ON buyer.id = o.buyer_id
            JOIN users seller ON seller.id = o.seller_id
            """
            + where_clause,
            params,
        ).fetchone()[0]
        raw_page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)
        total_pages = max(
            1, (total_orders + ADMIN_RECORDS_PAGE_SIZE - 1) // ADMIN_RECORDS_PAGE_SIZE
        )
        if page > total_pages:
            return redirect(
                url_for("admin_transactions", q=query, status=status, page=total_pages)
            )
        orders = db.execute(
            f"""
            SELECT o.*, l.title AS listing_title, l.image_filename,
                   buyer.full_name AS buyer_name, seller.full_name AS seller_name
            FROM orders o JOIN listings l ON l.id = o.listing_id
            JOIN users buyer ON buyer.id = o.buyer_id
            JOIN users seller ON seller.id = o.seller_id
            WHERE {' AND '.join(clauses)}
            ORDER BY o.created_at DESC, o.id DESC
            """,
            params,
        ).fetchall()
        return render_template(
            "transactions.html", orders=orders, view=view, status=status,
            statuses=ORDER_STATUSES,
        )

    @app.post("/orders/<int:order_id>/status")
    @login_required
    def update_order_status(order_id):
        db = get_db()
        user = db.execute(
            "SELECT role FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        if user is None:
            session.clear()
            return redirect(url_for("login"))
        is_admin = user["role"] == "admin"
        order = db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None or (
            not is_admin
            and session["user_id"] not in (order["buyer_id"], order["seller_id"])
        ):
            abort(404)
        actor_id = session["user_id"]
        allowed = available_order_actions(order, actor_id, is_admin=is_admin)
        form = OrderStatusForm()
        raw_action = (request.form.get("status") or "").strip()
        if raw_action == "declined" and actor_id != order["seller_id"] and not is_admin:
            flash("That order action is not allowed.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        if raw_action == "declined" and (actor_id == order["seller_id"] or is_admin):
            form.status.choices = allowed + [("declined", "Legacy Reject Order")]
        else:
            form.status.choices = allowed
        if not form.validate_on_submit():
            flash("Choose an available order action and check the reason.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        action = normalize_order_action(form.status.data)
        if action is None or action not in {value for value, _label in allowed}:
            flash("That order action is not allowed.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        reason = (form.status_reason.data or "").strip()
        if action == "cancelled" and not reason:
            if actor_id == order["seller_id"]:
                reason = "Rejected by seller."
            elif is_admin:
                reason = "Cancelled by administrator."
            else:
                reason = f"Cancelled by {'seller' if actor_id == order['seller_id'] else 'buyer'}."
        try:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """
                SELECT o.status, o.listing_id, o.quantity, o.stock_reserved,
                       o.buyer_id, o.seller_id, l.listing_type, l.title AS listing_title
                FROM orders AS o
                JOIN listings AS l ON l.id = o.listing_id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()
            if current is None or current["status"] != order["status"]:
                db.rollback()
                flash("This transaction has changed. Reload and try again.", "error")
                return redirect(url_for("order_detail", order_id=order_id))
            current_actions = available_order_actions(
                current, actor_id, is_admin=is_admin
            )
            if action not in {value for value, _label in current_actions}:
                db.rollback()
                flash("That order action is no longer available.", "error")
                return redirect(url_for("order_detail", order_id=order_id))
            updated = db.execute(
                """
                UPDATE orders SET status = ?, status_reason = ?
                WHERE id = ? AND status = ?
                """,
                (action, reason if action == "cancelled" else "", order_id, current["status"]),
            )
            if updated.rowcount != 1:
                db.rollback()
                flash("This transaction has changed. Reload and try again.", "error")
                return redirect(url_for("order_detail", order_id=order_id))
            if (
                action == "cancelled"
                and current["listing_type"] == "product"
                and current["stock_reserved"]
            ):
                db.execute(
                    """
                    UPDATE listings
                    SET stock = stock + ?,
                        status = CASE
                            WHEN status = 'removed' THEN status
                            ELSE 'active'
                        END
                    WHERE id = ?
                    """,
                    (current["quantity"], current["listing_id"]),
                )
            actor = db.execute(
                "SELECT full_name FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if is_admin:
                recipients = {order["buyer_id"], order["seller_id"]}
            else:
                recipients = {
                    order["seller_id"]
                    if actor_id == order["buyer_id"]
                    else order["buyer_id"]
                }
            if action == "cancelled" and actor_id == order["seller_id"]:
                notification_message = (
                    f"{current['listing_title']} was rejected by {actor['full_name']}."
                )
            elif action == "cancelled":
                notification_message = (
                    f"{current['listing_title']} was cancelled by {actor['full_name']}."
                )
            elif action == "completed" and actor_id == order["seller_id"]:
                notification_message = (
                    f"{current['listing_title']} was marked completed by "
                    f"{actor['full_name']}."
                )
            else:
                notification_message = {
                    "confirmed": f"{current['listing_title']} was confirmed by {actor['full_name']}.",
                    "preparing": f"{current['listing_title']} is being prepared by {actor['full_name']}.",
                    "ready_for_pickup": f"{current['listing_title']} is ready for pickup.",
                    "completed": f"Receipt for {current['listing_title']} is ready.",
                }[action]
            for recipient_id in recipients:
                create_notification(
                    db,
                    recipient_id,
                    actor_id,
                    "order",
                    notification_message,
                    order_id=order_id,
                )
            db.execute(
                """
                INSERT INTO order_events
                    (order_id, actor_id, old_status, new_status, is_admin)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, actor_id, current["status"], action, int(is_admin)),
            )
            if is_admin:
                db.execute(
                    """
                    INSERT INTO admin_actions
                        (admin_id, action, target_type, target_id, details)
                    VALUES (?, 'update_order_status', 'order', ?, ?)
                    """,
                    (actor_id, order_id, f"{current['status']} -> {action}"),
                )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not update transaction status.")
            flash("We couldn't update the transaction. Please try again.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        success_messages = {
            "confirmed": "Order confirmed. The buyer has been notified.",
            "preparing": "The buyer has been notified that the order is being prepared.",
            "ready_for_pickup": "The buyer has been notified that the order is ready for pickup.",
            "completed": (
                "Transaction marked complete."
                if actor_id == order["seller_id"]
                else "Receipt confirmed. The order is complete."
            ),
            "cancelled": (
                "Order rejected. The seller did not accept this order. Any reserved stock has been restored."
                if actor_id == order["seller_id"]
                else "Order cancelled. Any reserved stock has been restored."
            ),
        }
        flash(success_messages[action], "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.post("/orders/<int:order_id>/review")
    @login_required
    def submit_review(order_id):
        form = ReviewForm()
        order = get_db().execute(
            "SELECT buyer_id, seller_id, status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if order is None:
            abort(404)
        if (
            order["buyer_id"] != session["user_id"]
            or order["status"] != "completed"
        ):
            abort(403)
        if not form.validate_on_submit():
            flash("Provide a rating and feedback to submit your review.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        feedback = form.feedback.data.strip()
        try:
            rating = int(form.rating.data)
        except (TypeError, ValueError):
            rating = 0
        if not 1 <= rating <= 5 or not feedback or len(feedback) > 2000:
            flash("Enter a rating from 1 to 5 and feedback up to 2,000 characters.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        try:
            db = get_db()
            db.execute(
                """
                INSERT INTO reviews (order_id, buyer_id, seller_id, rating, feedback)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, order["buyer_id"], order["seller_id"], rating, feedback),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("A review has already been submitted for this transaction.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not save transaction review.")
            flash("We couldn't save your review. Please try again.", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        flash("Your rating and feedback were submitted.", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.post("/listings/<int:listing_id>/report")
    @login_required
    def report_listing(listing_id):
        form = ReportForm()
        listing = get_db().execute(
            "SELECT id FROM listings WHERE id = ? AND status != 'removed'",
            (listing_id,),
        ).fetchone()
        if listing is None:
            abort(404)
        if not form.validate_on_submit():
            flash("Choose a valid report reason and try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        explanation = (form.explanation.data or "").strip()
        if form.reason.data not in dict(REPORT_REASONS) or len(explanation) > 2000:
            flash("Choose a valid report reason and try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        try:
            db = get_db()
            db.execute(
                """
                INSERT INTO listing_reports (listing_id, reporter_id, reason, explanation)
                VALUES (?, ?, ?, ?)
                """,
                (listing_id, session["user_id"], form.reason.data, explanation),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("You already have an open report for this listing.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not save listing report.")
            flash("We couldn't submit the report. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        flash("Your report was submitted for administrator review.", "success")
        return redirect(url_for("listing_detail", listing_id=listing_id))

    @app.post("/listings/<int:listing_id>/archive")
    @login_required
    def archive_own_listing(listing_id):
        form = ArchiveListingForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute(
                "SELECT seller_id, status FROM listings WHERE id = ?",
                (listing_id,),
            ).fetchone()
            if listing is None:
                db.rollback()
                abort(404)
            if listing["seller_id"] != session["user_id"]:
                db.rollback()
                abort(403)
            active_order = db.execute(
                """
                SELECT 1 FROM orders
                WHERE listing_id = ? AND status IN ('pending', 'confirmed') LIMIT 1
                """,
                (listing_id,),
            ).fetchone()
            if active_order:
                db.rollback()
                flash("Resolve pending or confirmed orders before archiving this listing.", "error")
                return redirect(url_for("listing_detail", listing_id=listing_id))
            db.execute(
                "UPDATE listings SET status = 'removed' WHERE id = ?",
                (listing_id,),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not archive seller listing.")
            flash("We couldn't archive this listing. Please try again.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        flash("Your listing was archived; transaction history was preserved.", "success")
        return redirect(url_for("index"))

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        db = get_db()
        stats = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM users WHERE approval_status = 'pending')
                    AS pending_registrations,
                (SELECT COUNT(*) FROM listings
                 WHERE status = 'active' OR (status = 'sold' AND stock = 0))
                    AS active_listings,
                (SELECT COUNT(*) FROM listing_reports
                 WHERE status IN ('open', 'pending'))
                    AS pending_reports,
                (SELECT COUNT(*) FROM orders) AS transactions,
                (SELECT COUNT(*) FROM orders WHERE status = 'pending')
                    AS pending_transactions,
                (SELECT COUNT(*) FROM orders WHERE status = 'completed')
                    AS completed_transactions
            """
        ).fetchone()
        return render_template("admin/dashboard.html", stats=stats)

    def admin_action_return_values():
        values = {}
        query = request.form.get("q", "").strip()[:100]
        if query:
            values["q"] = query
        role = request.form.get("role", "")
        if role in {"student", "admin"}:
            values["role"] = role
        approval_status = request.form.get("status", "")
        if approval_status in {"pending", "approved", "rejected"}:
            values["status"] = approval_status
        elif approval_status in ORDER_STATUSES:
            values["status"] = approval_status
        elif approval_status in {"resolved", "dismissed"}:
            values["status"] = approval_status
        ban_status = request.form.get("ban", "")
        if ban_status in {"active", "banned"}:
            values["ban"] = ban_status
        category = request.form.get("category", "")
        if category in LISTING_CATEGORIES:
            values["category"] = category
        page = request.form.get("page", "")
        if re.fullmatch(r"[1-9][0-9]{0,8}", page) and page != "1":
            values["page"] = page
        return values

    @app.get("/admin/registrations")
    @admin_required
    def admin_registrations():
        raw_page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)
        db = get_db()
        total_registrations = db.execute(
            """
            SELECT COUNT(*) FROM users
            WHERE role = 'student' AND approval_status = 'pending'
            """
        ).fetchone()[0]
        total_overdue = db.execute(
            """
            SELECT COUNT(*) FROM users
            WHERE role = 'student' AND approval_status = 'pending'
              AND datetime(approval_requested_at, '+48 hours')
                  <= CURRENT_TIMESTAMP
            """
        ).fetchone()[0]
        total_pages = max(
            1,
            (total_registrations + ADMIN_RECORDS_PAGE_SIZE - 1)
            // ADMIN_RECORDS_PAGE_SIZE,
        )
        if page > total_pages:
            return redirect(
                url_for("admin_registrations", page=total_pages)
            )
        registrations = db.execute(
            """
            SELECT id, username, full_name, email, certificate_filename,
                   approval_requested_at,
                   CASE
                       WHEN datetime(approval_requested_at, '+48 hours')
                            <= CURRENT_TIMESTAMP THEN 1
                       ELSE 0
                   END AS review_overdue
            FROM users
            WHERE role = 'student' AND approval_status = 'pending'
            ORDER BY review_overdue DESC, approval_requested_at ASC, id ASC
            LIMIT ? OFFSET ?
            """
            ,
            [ADMIN_RECORDS_PAGE_SIZE, (page - 1) * ADMIN_RECORDS_PAGE_SIZE],
        ).fetchall()
        documents_by_user = {}
        if registrations:
            user_ids = [student["id"] for student in registrations]
            placeholders = ",".join("?" for _ in user_ids)
            documents = db.execute(
                f"""
                SELECT id, user_id, filename
                FROM registration_documents
                WHERE user_id IN ({placeholders})
                ORDER BY position ASC
                """,
                user_ids,
            ).fetchall()
            for document in documents:
                documents_by_user.setdefault(document["user_id"], []).append(document)
        return render_template(
            "admin/registrations.html",
            registrations=registrations,
            documents_by_user=documents_by_user,
            total_registrations=total_registrations,
            total_overdue=total_overdue,
            page=page,
            total_pages=total_pages,
            registration_decision_form=RegistrationDecisionForm(),
        )

    @app.get("/admin/registrations/<int:user_id>/certificate")
    @admin_required
    def admin_registration_certificate(user_id):
        user = get_db().execute(
            """
            SELECT COALESCE(
                (
                    SELECT registration_documents.filename
                    FROM registration_documents
                    WHERE registration_documents.user_id = users.id
                    ORDER BY registration_documents.position ASC
                    LIMIT 1
                ),
                users.certificate_filename
            ) AS filename
            FROM users
            WHERE users.id = ? AND role = 'student' AND approval_status = 'pending'
            """,
            (user_id,),
        ).fetchone()
        if (
            user is None
            or not user["filename"]
            or not re.fullmatch(AVATAR_FILENAME_PATTERN, user["filename"])
        ):
            abort(404)
        return send_from_directory(
            current_app.config["CERTIFICATE_UPLOAD_FOLDER"],
            user["filename"],
            mimetype="image/png",
        )

    @app.get(
        "/admin/registrations/<int:user_id>/certificates/<int:document_id>"
    )
    @admin_required
    def admin_registration_document(user_id, document_id):
        document = get_db().execute(
            """
            SELECT registration_documents.filename
            FROM registration_documents
            JOIN users ON users.id = registration_documents.user_id
            WHERE registration_documents.id = ? AND users.id = ?
              AND users.role = 'student' AND users.approval_status = 'pending'
            """,
            (document_id, user_id),
        ).fetchone()
        if (
            document is None
            or not re.fullmatch(AVATAR_FILENAME_PATTERN, document["filename"])
        ):
            abort(404)
        return send_from_directory(
            current_app.config["CERTIFICATE_UPLOAD_FOLDER"],
            document["filename"],
            mimetype="image/png",
        )

    def decide_student_registration(user_id, approval_status):
        form = RegistrationDecisionForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("admin_registrations"))

        reason = (form.reason.data or "").strip() if approval_status == "rejected" else ""
        db = get_db()
        removed_files = {}
        try:
            db.execute("BEGIN IMMEDIATE")
            registration = db.execute(
                """
                SELECT certificate_filename
                FROM users
                WHERE id = ? AND role = 'student' AND approval_status = 'pending'
                """,
                (user_id,),
            ).fetchone()
            if registration is None:
                db.rollback()
                flash("That student registration is no longer pending.", "error")
                return redirect(url_for("admin_registrations"))
            document_rows = db.execute(
                """
                SELECT filename FROM registration_documents
                WHERE user_id = ? ORDER BY position
                """,
                (user_id,),
            ).fetchall()
            filenames = [row["filename"] for row in document_rows]
            filenames.append(registration["certificate_filename"])
            removed_files = remove_registration_files(filenames)
            cursor = db.execute(
                """
                UPDATE users
                SET approval_status = ?, approval_decided_at = CURRENT_TIMESTAMP,
                    certificate_filename = NULL
                WHERE id = ? AND role = 'student' AND approval_status = 'pending'
                """,
                (approval_status, user_id),
            )
            if cursor.rowcount != 1:
                db.rollback()
                flash("That student registration is no longer pending.", "error")
                return redirect(url_for("admin_registrations"))
            decision_at = db.execute(
                "SELECT approval_decided_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()["approval_decided_at"]
            db.execute(
                """
                INSERT INTO admin_actions (admin_id, action, target_type, target_id, details)
                VALUES (?, ?, 'student_registration', ?, ?)
                """,
                (
                    session["user_id"],
                    f"{approval_status}_student_registration",
                    user_id,
                    reason,
                ),
            )
            body = (
                "APPROVED: Your student registration was approved on "
                f"{format_datetime(decision_at)}. You may now sign in and access "
                "the DangalBasket marketplace."
                if approval_status == "approved"
                else "REJECTED: Your student registration was not approved on "
                f"{format_datetime(decision_at)}. Review the reason, if provided, "
                "and resubmit your registration documents."
            )
            if reason:
                body += f" Reason: {reason}"
            create_notification(
                db, user_id, session["user_id"], "account", body
            )
            db.execute(
                "DELETE FROM registration_documents WHERE user_id = ?", (user_id,)
            )
            db.commit()
        except (OSError, ValueError, sqlite3.Error):
            db.rollback()
            if removed_files:
                restore_registration_files(removed_files)
            current_app.logger.exception(
                "Could not update student registration and remove its documents."
            )
            flash(
                "We couldn't complete the registration decision and safely remove "
                "its documents. No decision was recorded; please try again.",
                "error",
            )
            return redirect(url_for("admin_registrations"))

        outcome = "approved" if approval_status == "approved" else "rejected"
        flash(
            f"Student registration {outcome}."
            + (f" Reason recorded: {reason}" if reason else ""),
            "success",
        )
        return redirect(url_for("admin_registrations"))

    @app.post("/admin/registrations/<int:user_id>/approve")
    @admin_required
    def approve_student_registration(user_id):
        return decide_student_registration(user_id, "approved")

    @app.post("/admin/registrations/<int:user_id>/reject")
    @admin_required
    def reject_student_registration(user_id):
        return decide_student_registration(user_id, "rejected")

    def admin_users_return_values():
        values = {}
        query = request.form.get("q", "").strip()[:100]
        role = request.form.get("role", "all")
        approval_status = request.form.get("status", "all")
        ban_status = request.form.get("ban", "all")
        page = request.form.get("page", "1")
        if query:
            values["q"] = query
        if role in {"student", "admin"}:
            values["role"] = role
        if approval_status in {"pending", "approved", "rejected"}:
            values["status"] = approval_status
        if ban_status in {"active", "banned"}:
            values["ban"] = ban_status
        if re.fullmatch(r"[1-9][0-9]{0,8}", page) and page != "1":
            values["page"] = page
        return values

    @app.get("/admin/users")
    @admin_required
    def admin_users():
        query = request.args.get("q", "").strip()[:100]
        role = request.args.get("role", "all")
        approval_status = request.args.get("status", "all")
        ban_status = request.args.get("ban", "all")
        raw_page = request.args.get("page", "1")
        if role not in {"all", "student", "admin"}:
            abort(400)
        if approval_status not in {"all", "pending", "approved", "rejected"}:
            abort(400)
        if ban_status not in {"all", "active", "banned"}:
            abort(400)
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)
        clauses = []
        params = []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(username LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\' "
                "OR email LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        if role != "all":
            clauses.append("u.role = ?")
            params.append(role)
        if approval_status != "all":
            clauses.append("u.approval_status = ?")
            params.append(approval_status)
        if ban_status == "active":
            clauses.append("u.banned_at IS NULL")
        elif ban_status == "banned":
            clauses.append("u.banned_at IS NOT NULL")

        db = get_db()
        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_users = db.execute(
            "SELECT COUNT(*) FROM users AS u" + where_clause,
            params,
        ).fetchone()[0]
        total_pages = max(
            1, (total_users + ADMIN_USERS_PAGE_SIZE - 1) // ADMIN_USERS_PAGE_SIZE
        )
        if page > total_pages:
            return redirect(
                url_for(
                    "admin_users",
                    q=query,
                    role=role,
                    status=approval_status,
                    ban=ban_status,
                    page=total_pages,
                )
            )

        users = db.execute(
            """
            SELECT u.id, u.username, u.full_name,
                   COALESCE(NULLIF(u.email, ''), 'Not provided') AS email,
                   u.role, u.approval_status, u.banned_at, u.ban_reason, u.created_at,
                   COALESCE(report_counts.report_count, 0) AS report_count,
                   COALESCE(report_counts.pending_report_count, 0)
                       AS pending_report_count
            FROM users AS u
            LEFT JOIN (
                SELECT l.seller_id,
                       COUNT(r.id) AS report_count,
                       SUM(CASE WHEN r.status IN ('open', 'pending') THEN 1 ELSE 0 END)
                           AS pending_report_count
                FROM listings AS l
                JOIN listing_reports AS r ON r.listing_id = l.id
                GROUP BY l.seller_id
            ) AS report_counts ON report_counts.seller_id = u.id
            """
            + where_clause
            + """
            ORDER BY u.created_at DESC, u.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, ADMIN_USERS_PAGE_SIZE, (page - 1) * ADMIN_USERS_PAGE_SIZE],
        ).fetchall()
        return render_template(
            "admin/users.html",
            users=users,
            query=query,
            role_filter=role,
            approval_filter=approval_status,
            ban_filter=ban_status,
            total_users=total_users,
            page=page,
            total_pages=total_pages,
            page_size=ADMIN_USERS_PAGE_SIZE,
            ban_user_form=BanUserForm(),
            user_action_form=LogoutForm(),
        )

    def update_user_ban(user_id, banned):
        form = BanUserForm() if banned else LogoutForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("admin_users", **admin_users_return_values()))

        reason = form.reason.data.strip() if banned else ""
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute(
                "SELECT role, banned_at, ban_reason FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                db.rollback()
                abort(404)
            if banned and user["role"] == "admin":
                db.rollback()
                flash("Administrator accounts cannot be banned.", "error")
                return redirect(url_for("admin_users", **admin_users_return_values()))
            if banned and user["banned_at"] is not None:
                db.rollback()
                flash("That account is already banned.", "error")
                return redirect(url_for("admin_users", **admin_users_return_values()))
            if not banned and user["banned_at"] is None:
                db.rollback()
                flash("That account is not currently banned.", "error")
                return redirect(url_for("admin_users", **admin_users_return_values()))

            if banned:
                db.execute(
                    """
                    UPDATE users
                    SET banned_at = CURRENT_TIMESTAMP, banned_by = ?, ban_reason = ?
                    WHERE id = ?
                    """,
                    (session["user_id"], reason, user_id),
                )
                action = "ban_user"
                details = reason
            else:
                db.execute(
                    """
                    UPDATE users
                    SET banned_at = NULL, banned_by = NULL, ban_reason = ''
                    WHERE id = ?
                    """,
                    (user_id,),
                )
                action = "reinstate_user"
                details = user["ban_reason"]
            db.execute(
                """
                INSERT INTO admin_actions (admin_id, action, target_type, target_id, details)
                VALUES (?, ?, 'user', ?, ?)
                """,
                (session["user_id"], action, user_id, details),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception(
                "Administrator could not %s user account.",
                "ban" if banned else "reinstate",
            )
            flash("We couldn't update that account. Please try again.", "error")
            return redirect(url_for("admin_users", **admin_users_return_values()))

        flash(
            "Account banned." if banned else "Account reinstated.",
            "success",
        )
        return redirect(url_for("admin_users", **admin_users_return_values()))

    @app.post("/admin/users/<int:user_id>/ban")
    @admin_required
    def ban_user(user_id):
        return update_user_ban(user_id, banned=True)

    @app.post("/admin/users/<int:user_id>/reinstate")
    @admin_required
    def reinstate_user(user_id):
        return update_user_ban(user_id, banned=False)

    @app.post("/admin/users/<int:user_id>/promote")
    @admin_required
    def promote_user_to_admin(user_id):
        form = LogoutForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("admin_users", **admin_users_return_values()))

        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute(
                "SELECT role, approval_status, banned_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                db.rollback()
                abort(404)
            if user["role"] == "admin":
                db.rollback()
                flash("That user is already an administrator.", "error")
                return redirect(url_for("admin_users", **admin_users_return_values()))
            if user["approval_status"] != "approved":
                db.rollback()
                flash(
                    "Approve the student's registration before promoting them to administrator.",
                    "error",
                )
                return redirect(url_for("admin_users", **admin_users_return_values()))
            if user["banned_at"] is not None:
                db.rollback()
                flash("Reinstate the account before promoting it to administrator.", "error")
                return redirect(url_for("admin_users", **admin_users_return_values()))

            db.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO admin_actions (admin_id, action, target_type, target_id, details)
                VALUES (?, 'promote_user_to_admin', 'user', ?, 'student -> admin')
                """,
                (session["user_id"], user_id),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not promote user to administrator.")
            flash("We couldn't promote that user. Please try again.", "error")
            return redirect(url_for("admin_users", **admin_users_return_values()))

        flash("User promoted to administrator.", "success")
        return redirect(url_for("admin_users", **admin_users_return_values()))

    @app.get("/admin/listings")
    @admin_required
    def admin_listings():
        query = request.args.get("q", "").strip()[:100]
        category = request.args.get("category", "")
        status = request.args.get("status", "")
        clauses = []
        params = []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(l.title LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\' "
                "OR u.full_name LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        if category in LISTING_CATEGORIES:
            clauses.append("l.category = ?")
            params.append(category)
        else:
            category = ""
        if status in {"active", "sold", "removed"}:
            clauses.append("l.status = ?")
            params.append(status)
        else:
            status = ""
        db = get_db()
        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_listings = db.execute(
            "SELECT COUNT(*) FROM listings AS l JOIN users AS u ON u.id = l.seller_id"
            + where_clause,
            params,
        ).fetchone()[0]
        raw_page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)
        total_pages = max(
            1, (total_listings + ADMIN_RECORDS_PAGE_SIZE - 1) // ADMIN_RECORDS_PAGE_SIZE
        )
        if page > total_pages:
            return redirect(
                url_for(
                    "admin_listings",
                    q=query,
                    category=category,
                    status=status,
                    page=total_pages,
                )
            )
        listings = db.execute(
            """
            SELECT l.*, u.username AS seller_username, u.full_name AS seller_name
            FROM listings l JOIN users u ON u.id = l.seller_id
            """
            + where_clause
            + """
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, ADMIN_RECORDS_PAGE_SIZE, (page - 1) * ADMIN_RECORDS_PAGE_SIZE],
        ).fetchall()
        return render_template(
            "admin/listings.html", listings=listings, query=query,
            category=category, status=status, categories=LISTING_CATEGORIES,
            total_listings=total_listings, page=page, total_pages=total_pages,
        )

    @app.post("/admin/listings/<int:listing_id>/archive")
    @admin_required
    def admin_archive_listing(listing_id):
        form = ArchiveListingForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("admin_listings", **admin_action_return_values()))
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute(
                "SELECT id, status FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if listing is None:
                db.rollback()
                abort(404)
            db.execute("UPDATE listings SET status = 'removed' WHERE id = ?", (listing_id,))
            db.execute(
                """
                INSERT INTO admin_actions
                    (admin_id, action, target_type, target_id, details)
                VALUES (?, 'archive_listing', 'listing', ?, ?)
                """,
                (session["user_id"], listing_id, f"Previous status: {listing['status']}"),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Administrator could not archive listing.")
            flash("We couldn't archive the listing. Please try again.", "error")
            return redirect(url_for("admin_listings", **admin_action_return_values()))
        flash("Listing archived. Historical records were retained.", "success")
        return redirect(url_for("admin_listings", **admin_action_return_values()))

    @app.get("/admin/reports")
    @admin_required
    def admin_reports():
        status = request.args.get("status", "")
        if status not in {"open", "pending", "resolved", "dismissed"}:
            status = ""
        clauses = []
        params = []
        if status in {"open", "pending"}:
            clauses.append("r.status IN ('open', 'pending')")
        elif status:
            clauses.append("r.status = ?")
            params.append(status)
        db = get_db()
        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_reports = db.execute(
            "SELECT COUNT(*) FROM listing_reports AS r" + where_clause,
            params,
        ).fetchone()[0]
        raw_page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)
        total_pages = max(
            1, (total_reports + ADMIN_RECORDS_PAGE_SIZE - 1) // ADMIN_RECORDS_PAGE_SIZE
        )
        if page > total_pages:
            return redirect(
                url_for("admin_reports", status=status, page=total_pages)
            )
        reports = get_db().execute(
            """
            SELECT r.*, l.title AS listing_title, l.status AS listing_status,
                   l.listing_type, l.category, l.description, l.price_cents,
                   l.stock, l.image_filename,
                   reporter.username AS reporter_username,
                   reporter.full_name AS reporter_name,
                   seller.username AS seller_username,
                   seller.full_name AS seller_name,
                   (SELECT COUNT(*)
                    FROM listing_reports seller_reports
                    JOIN listings seller_listings
                      ON seller_listings.id = seller_reports.listing_id
                    WHERE seller_listings.seller_id = seller.id)
                       AS seller_report_count,
                   resolver.username AS resolver_username
            FROM listing_reports r JOIN listings l ON l.id = r.listing_id
            JOIN users reporter ON reporter.id = r.reporter_id
            JOIN users seller ON seller.id = l.seller_id
            LEFT JOIN users resolver ON resolver.id = r.resolved_by
            """
            + where_clause
            + """
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, ADMIN_RECORDS_PAGE_SIZE, (page - 1) * ADMIN_RECORDS_PAGE_SIZE],
        ).fetchall()
        return render_template(
            "admin/reports.html",
            reports=reports,
            status=status,
            report_reasons=dict(REPORT_REASONS),
            total_reports=total_reports,
            page=page,
            total_pages=total_pages,
        )

    def finalize_report(report_id, status):
        form = ResolveReportForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("admin_reports", **admin_action_return_values()))
        details = (form.resolution_details.data or "").strip()
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """
                UPDATE listing_reports
                SET status = ?, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP,
                    resolution_details = ?
                WHERE id = ? AND status IN ('open', 'pending')
                """,
                (status, session["user_id"], details, report_id),
            )
            if cursor.rowcount != 1:
                db.rollback()
                flash("That report is missing or already processed.", "error")
                return redirect(url_for("admin_reports", **admin_action_return_values()))
            db.execute(
                """
                INSERT INTO admin_actions
                    (admin_id, action, target_type, target_id, details)
                VALUES (?, ?, 'report', ?, ?)
                """,
                (
                    session["user_id"],
                    "resolve_report" if status == "resolved" else "dismiss_report",
                    report_id,
                    details,
                ),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Administrator could not update report.")
            flash("We couldn't update the report. Please try again.", "error")
            return redirect(url_for("admin_reports", **admin_action_return_values()))
        flash(
            "Report marked as resolved."
            if status == "resolved"
            else "Report dismissed.",
            "success",
        )
        return redirect(url_for("admin_reports", **admin_action_return_values()))

    @app.post("/admin/reports/<int:report_id>/resolve")
    @admin_required
    def resolve_report(report_id):
        return finalize_report(report_id, "resolved")

    @app.post("/admin/reports/<int:report_id>/dismiss")
    @admin_required
    def dismiss_report(report_id):
        return finalize_report(report_id, "dismissed")

    @app.post("/admin/reports/<int:report_id>/archive-listing")
    @admin_required
    def archive_reported_listing(report_id):
        form = ResolveReportForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("admin_reports", **admin_action_return_values()))
        details = (form.resolution_details.data or "").strip()
        resolution_details = "Listing archived after report review."
        if details:
            resolution_details = f"{resolution_details} {details}"
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            report = db.execute(
                """
                SELECT listing_id FROM listing_reports
                WHERE id = ? AND status IN ('open', 'pending')
                """,
                (report_id,),
            ).fetchone()
            if report is None:
                db.rollback()
                flash("That report is missing or already processed.", "error")
                return redirect(url_for("admin_reports", **admin_action_return_values()))
            listing_id = report["listing_id"]
            listing_update = db.execute(
                "UPDATE listings SET status = 'removed' WHERE id = ?",
                (listing_id,),
            )
            if listing_update.rowcount != 1:
                db.rollback()
                abort(404)
            report_update = db.execute(
                """
                UPDATE listing_reports
                SET status = 'resolved', resolved_by = ?, resolved_at = CURRENT_TIMESTAMP,
                    resolution_details = ?
                WHERE id = ? AND status IN ('open', 'pending')
                """,
                (session["user_id"], resolution_details, report_id),
            )
            if report_update.rowcount != 1:
                db.rollback()
                flash("That report is missing or already processed.", "error")
                return redirect(url_for("admin_reports", **admin_action_return_values()))
            db.execute(
                """
                INSERT INTO admin_actions
                    (admin_id, action, target_type, target_id, details)
                VALUES (?, 'archive_reported_listing', 'listing', ?, ?)
                """,
                (session["user_id"], listing_id, f"Report #{report_id}. {resolution_details}"),
            )
            db.execute(
                """
                INSERT INTO admin_actions
                    (admin_id, action, target_type, target_id, details)
                VALUES (?, 'resolve_report', 'report', ?, ?)
                """,
                (session["user_id"], report_id, resolution_details),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Administrator could not archive reported listing.")
            flash("We couldn't archive the listing. Please try again.", "error")
            return redirect(url_for("admin_reports", **admin_action_return_values()))
        flash("Listing archived and report resolved. Historical records were retained.", "success")
        return redirect(url_for("admin_reports", **admin_action_return_values()))

    @app.get("/admin/transactions")
    @admin_required
    def admin_transactions():
        query = request.args.get("q", "").strip()[:100]
        status = request.args.get("status", "")
        clauses = []
        params = []
        if query:
            if query.isdecimal():
                clauses.append("o.id = ?")
                params.append(int(query))
            else:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                clauses.append(
                    "(buyer.username LIKE ? ESCAPE '\\' OR buyer.full_name LIKE ? ESCAPE '\\' "
                    "OR seller.username LIKE ? ESCAPE '\\' OR seller.full_name LIKE ? ESCAPE '\\' "
                    "OR l.title LIKE ? ESCAPE '\\')"
                )
                params.extend([pattern, pattern, pattern, pattern, pattern])
        if status in ORDER_STATUSES:
            clauses.append("o.status = ?")
            params.append(status)
        else:
            status = ""
        db = get_db()
        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_orders = db.execute(
            """
            SELECT COUNT(*)
            FROM orders o JOIN listings l ON l.id = o.listing_id
            JOIN users buyer ON buyer.id = o.buyer_id
            JOIN users seller ON seller.id = o.seller_id
            """
            + where_clause,
            params,
        ).fetchone()[0]
        raw_page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            abort(400)
        page = int(raw_page)
        total_pages = max(
            1, (total_orders + ADMIN_RECORDS_PAGE_SIZE - 1) // ADMIN_RECORDS_PAGE_SIZE
        )
        if page > total_pages:
            return redirect(
                url_for("admin_transactions", q=query, status=status, page=total_pages)
            )
        orders = db.execute(
            """
            SELECT o.*, l.title AS listing_title,
                   buyer.username AS buyer_username, buyer.full_name AS buyer_name,
                   seller.username AS seller_username, seller.full_name AS seller_name
            FROM orders o JOIN listings l ON l.id = o.listing_id
            JOIN users buyer ON buyer.id = o.buyer_id
            JOIN users seller ON seller.id = o.seller_id
            """
            + where_clause
            + """
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, ADMIN_RECORDS_PAGE_SIZE, (page - 1) * ADMIN_RECORDS_PAGE_SIZE],
        ).fetchall()
        return render_template(
            "admin/transactions.html", orders=orders, query=query,
            status=status, statuses=ORDER_STATUSES,
            total_orders=total_orders, page=page, total_pages=total_pages,
        )

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        db = get_db()
        user = db.execute(
            """
            SELECT id, username, full_name, bio, role, avatar_filename
            FROM users WHERE id = ?
            """,
            (session["user_id"],),
        ).fetchone()
        if user is None:
            session.clear()
            flash("Please sign in to view that page.", "error")
            return redirect(url_for("login"))

        name_form = ProfileForm()
        photo_form = ProfilePhotoForm()
        remove_form = RemovePhotoForm()
        photo_error = None
        if request.method == "GET":
            name_form.bio.data = user["bio"]
        if request.method == "POST":
            valid_details = name_form.validate()
            full_name = (name_form.full_name.data or "").strip()
            bio = (
                (name_form.bio.data or "").strip()
                if "bio" in request.form
                else user["bio"]
            )
            if not full_name and "Full name is required." not in name_form.full_name.errors:
                name_form.full_name.errors.append("Full name is required.")
                valid_details = False

            photo = photo_form.photo.data
            normalized_photo = None
            photo_error = None
            if photo and photo.filename:
                normalized_photo, photo_error = normalize_avatar_upload(photo)

            if valid_details and not photo_error and full_name:
                filename = f"{uuid.uuid4().hex}.png" if normalized_photo else None
                upload_path = (
                    Path(current_app.config["UPLOAD_FOLDER"]) / filename
                    if filename
                    else None
                )
                try:
                    if upload_path and normalized_photo:
                        upload_path.write_bytes(normalized_photo)
                    db.execute(
                        """
                        UPDATE users
                        SET full_name = ?, bio = ?,
                            avatar_filename = COALESCE(?, avatar_filename)
                        WHERE id = ?
                        """,
                        (full_name, bio, filename, user["id"]),
                    )
                    db.commit()
                    session["full_name"] = full_name
                    if filename:
                        delete_avatar_file(user["avatar_filename"])
                    flash("Profile updated — Your profile has been updated.", "success")
                    return redirect(url_for("profile"))
                except (OSError, sqlite3.Error):
                    db.rollback()
                    if filename:
                        delete_avatar_file(filename)
                    current_app.logger.exception("Database error updating profile.")
                    flash("We couldn't save your profile. Please try again.", "error")
            elif getattr(name_form, "csrf_token", None) and name_form.csrf_token.errors:
                flash("Your request could not be verified. Please reload and try again.", "error")

        return render_template(
            "profile.html",
            user=user,
            has_avatar=avatar_is_available(user["avatar_filename"]),
            avatar_size_limit=avatar_size_limit_label(),
            avatar_max_bytes=current_app.config["MAX_AVATAR_BYTES"],
            name_form=name_form,
            photo_form=photo_form,
            photo_error=photo_error,
            remove_form=remove_form,
            logout_form=LogoutForm(),
        )

    @app.post("/profile/photo")
    @login_required
    def upload_profile_photo():
        form = ProfilePhotoForm()
        if not form.validate_on_submit():
            if getattr(form, "csrf_token", None) and form.csrf_token.errors:
                flash("Your request could not be verified. Please reload and try again.", "error")
            else:
                flash("Choose a profile photo to upload.", "error")
            return redirect(url_for("profile"))

        photo = form.photo.data
        if not photo or not photo.filename:
            flash("Choose a profile photo to upload.", "error")
            return redirect(url_for("profile"))
        normalized, photo_error = normalize_avatar_upload(photo)
        if photo_error:
            flash(photo_error, "error")
            return redirect(url_for("profile"))

        filename = f"{uuid.uuid4().hex}.png"
        upload_path = Path(current_app.config["UPLOAD_FOLDER"]) / filename
        try:
            upload_path.write_bytes(normalized)
            db = get_db()
            current = db.execute(
                "SELECT avatar_filename FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
            if current is None:
                delete_avatar_file(filename)
                session.clear()
                return redirect(url_for("login"))
            db.execute(
                "UPDATE users SET avatar_filename = ? WHERE id = ?",
                (filename, session["user_id"]),
            )
            db.commit()
        except (OSError, sqlite3.Error):
            delete_avatar_file(filename)
            current_app.logger.exception("Could not save profile photo.")
            flash("We couldn't update your profile photo. Please try again.", "error")
            return redirect(url_for("profile"))

        delete_avatar_file(current["avatar_filename"])
        flash("Your profile photo has been updated.", "success")
        return redirect(url_for("profile"))

    @app.post("/profile/photo/remove")
    @login_required
    def remove_profile_photo():
        form = RemovePhotoForm()
        if not form.validate_on_submit():
            flash("Your request could not be verified. Please try again.", "error")
            return redirect(url_for("profile"))
        db = get_db()
        try:
            current = db.execute(
                "SELECT avatar_filename FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
            if current is None:
                session.clear()
                return redirect(url_for("login"))
            db.execute(
                "UPDATE users SET avatar_filename = NULL WHERE id = ?",
                (session["user_id"],),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            current_app.logger.exception("Could not remove profile photo.")
            flash("We couldn't remove your profile photo. Please try again.", "error")
            return redirect(url_for("profile"))
        delete_avatar_file(current["avatar_filename"])
        flash("Your profile photo was removed.", "success")
        return redirect(url_for("profile"))

    @app.get("/users/<username>")
    def public_profile(username):
        db = get_db()
        user = db.execute(
            """
            SELECT id, username, full_name, bio, avatar_filename
            FROM users WHERE username = ?
            """,
            (username,),
        ).fetchone()
        if user is None:
            abort(404)
        stats = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM listings
                 WHERE seller_id = ? AND status != 'removed') AS total_listings,
                (SELECT COUNT(*) FROM listings
                 WHERE seller_id = ?
                   AND status = 'active'
                   AND (listing_type = 'service' OR stock > 0))
                    AS active_listings,
                (SELECT COUNT(*) FROM orders
                 WHERE seller_id = ? AND status = 'completed') AS completed_transactions,
                (SELECT AVG(rating) FROM reviews WHERE seller_id = ?) AS average_rating,
                (SELECT COUNT(*) FROM reviews WHERE seller_id = ?) AS rating_count
            """,
            (user["id"], user["id"], user["id"], user["id"], user["id"]),
        ).fetchone()
        listings = db.execute(
            """
            SELECT l.*, u.username AS seller_username
            FROM listings AS l JOIN users AS u ON u.id = l.seller_id
            WHERE l.seller_id = ?
              AND l.status = 'active'
              AND (l.listing_type = 'service' OR l.stock > 0)
            ORDER BY l.created_at DESC, l.id DESC
            """,
            (user["id"],),
        ).fetchall()
        reviews = db.execute(
            """
            SELECT r.rating, r.feedback, r.created_at, buyer.full_name AS buyer_name
            FROM reviews AS r JOIN users AS buyer ON buyer.id = r.buyer_id
            WHERE r.seller_id = ?
            ORDER BY r.created_at DESC, r.id DESC LIMIT 10
            """,
            (user["id"],),
        ).fetchall()
        return render_template(
            "public_profile.html",
            user=user,
            has_avatar=avatar_is_available(user["avatar_filename"]),
            stats=stats,
            listings=listings,
            reviews=reviews,
            logout_form=LogoutForm(),
        )

    @app.get("/profile-photos/<filename>")
    def profile_photo(filename):
        if not re.fullmatch(AVATAR_FILENAME_PATTERN, filename):
            abort(404)
        return send_from_directory(
            current_app.config["UPLOAD_FOLDER"],
            filename,
            mimetype="image/png",
            as_attachment=False,
            conditional=True,
        )

    @app.get("/listing-images/<filename>")
    def listing_image(filename):
        if not re.fullmatch(LISTING_FILENAME_PATTERN, filename):
            abort(404)
        return send_from_directory(
            current_app.config["LISTING_UPLOAD_FOLDER"],
            filename,
            mimetype="image/png",
            as_attachment=False,
            conditional=True,
        )

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error):
        if request.path.startswith("/listings"):
            flash(
                f"Listing images must be {listing_image_size_limit_label()} or smaller.",
                "error",
            )
        else:
            flash(
                f"Profile photos must be {avatar_size_limit_label()} or smaller.",
                "error",
            )
        if request.path.startswith("/profile"):
            return redirect(url_for("profile"))
        if request.path.startswith("/listings"):
            return redirect(url_for("create_listing"))
        return "Request is too large.", 413

    @app.errorhandler(404)
    def page_not_found(_error):
        return render_template("404.html"), 404

    return app


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    create_app().run(host=host, port=port, use_reloader=True)
