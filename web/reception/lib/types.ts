export const RECEPTION_TOKEN_COOKIE = "stay_device_token";
export const RECEPTION_SESSION_COOKIE = "stay_sessionid";

export type TenantOption = {
  id: number;
  name: string;
  slug: string;
};

export type ReservationStatus = "expected" | "checked_in" | "checked_out" | "canceled" | "no_show" | "pending" | "refused";

export type ReservationUnit = {
  id: number;
  sort_order: number;
  room_name: string;
  room: number | null;
  room_code?: string;
};

export type Reservation = {
  id: number;
  external_id: string;
  property_slug?: string;
  property_name?: string;
  room_name: string;
  room_codes?: string[];
  check_in_date: string;
  check_out_date: string;
  status: ReservationStatus;
  total_amount: string | null;
  currency: string;
  guests_count: number;
  primary_guest_name: string;
  primary_guest_nationality_iso2: string;
  expected_arrival_at?: string;
  guest_stated_arrival_at?: string | null;
  guest_stated_arrival_text?: string;
  evisitor_summary?: EvisitorSummary;
  evisitor_progress?: EvisitorProgress;
  guests?: GuestLite[];
};

export type Room = {
  id: number;
  code: string;
  room_type: number | null;
  room_type_name: string;
  is_active: boolean;
};

export type AppConfig = {
  tenant: { name: string; slug: string };
  properties: Array<{ name: string; slug: string; financial_report_recipients?: string }>;
  units?: Array<{ id: number; code: string; property_slug?: string; name?: string }>;
  channel_manager?: string;
  feature_flags: Record<string, boolean>;
  branding: Record<string, unknown>;
};

export type GuestLite = {
  id: number;
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  date_of_birth: string | null;
  sex: string;
  address: string;
  is_primary: boolean;
  nationality: string;
  document_number: string;
  document_type: string;
  date_of_issue: string | null;
  date_of_expiry: string | null;
  issuing_authority: string;
  personal_id_number: string;
  evisitor_status: string;
  evisitor_error: string;
  evisitor_required?: boolean;
  face_photo_url: string;
};

export type EvisitorSummary = "none" | "incomplete" | "complete" | "checked_out";

export type CheckinProgress = {
  required_slots: number;
  ready_slots: number;
  waiting_positions: number[];
  session_status: string | null;
  effective_status: string | null;
  last_activity_at: string | null;
  checkin_url: string | null;
};

export type EvisitorProgress = {
  required: number;
  sent: number;
  failed: number;
  pending: number;
};

export type EvisitorCheckinFailedGuest = {
  guest_id?: number;
  guest_name?: string;
  status?: string;
  message?: string;
  field_errors?: Record<string, string>;
};

export type EvisitorCheckinResult = {
  overall: "complete" | "partial" | "none" | "not_required";
  submitted: number;
  skipped: number;
  failed: number;
  validation_failed: number;
  failed_guests: EvisitorCheckinFailedGuest[];
  correlation_id?: string;
};

export type OfferSummary = {
  id: number;
  offer_number: string;
  issued_at: string;
  valid_until: string | null;
  email_sent_at: string | null;
  total?: string;
  currency?: string;
  buyer_name?: string;
  payment_reference?: string;
  public_access_token?: string;
};

export type InvoiceSummary = {
  id: number;
  invoice_number: string;
  fiscal_status: string;
  jir: string | null;
  zki: string | null;
  email_sent_at: string | null;
  total?: string;
  currency?: string;
};

export type ReservationVersionsResponse = {
  versions: Record<string, number>;
};

export type GuestMessageTimelineItem = {
  id: number;
  canonical_id?: number;
  source: "outbound" | "whatsapp" | "booking" | "inbound";
  whatsapp_source?: "cloud_api" | "business_app";
  direction: "inbound" | "outbound";
  channel: "email" | "whatsapp" | "booking";
  channels?: string[];
  body_text: string;
  created_at: string;
  status: string | null;
  sent_by_name: string | null;
  from_email: string | null;
  wa_me_url: string | null;
  message_type?: string | null;
  document_intake_job_id?: number | null;
};

export type GuestMessageChannelInfo = {
  available: boolean;
  to?: string;
  phone_raw?: string;
  phone_wa?: string;
  wa_me_url?: string;
  api_send?: boolean;
  session_open?: boolean;
  template_name?: string | null;
  template_available?: boolean;
};

export type GuestMessageChannels = Record<string, GuestMessageChannelInfo> & {
  default_channel?: string;
};

export type GuestMessageComposeResponse = {
  draft_id: number;
  body_text: string;
  language: string;
  llm_used: boolean;
  channels: GuestMessageChannels;
};

export type MessageThread = {
  reservation_id: number;
  booker_name: string;
  check_in: string | null;
  check_out: string | null;
  room_name: string;
  status: string;
  arrives_today: boolean;
  last_message_at: string | null;
  last_message_preview: string;
  last_channel: string;
  last_channels: string[];
  last_direction: string;
  needs_reply: boolean;
};

export type MessageThreadsListResponse = {
  page: number;
  page_size: number;
  total: number;
  needs_reply_count: number;
  threads: MessageThread[];
};

export type ChannexReviewScore = {
  category: string;
  score: number;
};

export type ChannexReview = {
  id: number;
  channex_review_id: string;
  reservation_id: number | null;
  booking_code: string | null;
  ota: string;
  ota_reservation_id?: string;
  guest_name: string;
  overall_score: number | null;
  scores: ChannexReviewScore[];
  tags: string[];
  content: string;
  content_localized?: string | null;
  content_is_translated?: boolean;
  display_language?: string | null;
  translation_available?: boolean;
  reply: string | null;
  is_replied: boolean;
  reply_published?: boolean;
  reply_pending_moderation?: boolean;
  suggested_reply_language?: string;
  is_hidden: boolean;
  expired_at: string | null;
  received_at: string | null;
  reply_sent_at: string | null;
  can_reply: boolean;
  can_submit_guest_review: boolean;
};

export type ChannexReviewsListResponse = {
  page: number;
  page_size: number;
  total: number;
  reviews: ChannexReview[];
};

export type ReservationReviewsResponse = {
  reservation_id: number;
  reviews: ChannexReview[];
};

export type ReservationDetail = Reservation & {
  units?: ReservationUnit[];
  guests: GuestLite[];
  booker_name: string;
  booker_email?: string;
  booker_phone: string;
  invoice_email?: string;
  invoice_summary?: InvoiceSummary | null;
  offer_summary?: OfferSummary | null;
  booking_code?: string;
  notes: string;
  source: string;
  import_source: string;
  pdf_imported_at: string | null;
  xls_imported_at: string | null;
  confirmation_pdf_url: string;
  evisitor_checkin?: EvisitorCheckinResult | null;
  checkin_progress?: CheckinProgress;
  check_in_allowed?: boolean;
  check_in_blocked_code?: "wrong_date" | "room_occupied" | "no_unit" | null;
  payment_status?: string;
  payment_status_key?: string;
  payment_provider?: string;
  commission_percent?: string | null;
  commission_amount?: string | null;
  nights_count?: number | null;
  booking_payout_received_at?: string | null;
  booking_payout_id?: string;
  booking_payout_net?: string | null;
  booking_payout_service_fee?: string | null;
  booking_payout_received?: boolean;
};

export type BookingPdfImportResult = ReservationDetail & {
  created?: boolean;
  skip_reason?: string;
};

export type CalendarReservation = {
  id: number;
  external_id: string;
  check_in_date: string;
  check_out_date: string;
  status: ReservationStatus;
  room_name: string;
  primary_guest_name: string;
  primary_guest_nationality_iso2: string;
};

export type CalendarBlock = {
  id: number | null;
  unit_id: number;
  unit_code: string;
  check_in: string;
  check_out: string;
  block_ref: string | null;
  reservation_id: number | null;
  can_unblock: boolean;
  source: "stay";
};

export type CalendarSelection = {
  roomId: number;
  date: string;
};

export type ChannelAvailabilityDay = {
  unit_id: number;
  date: string;
  availability: number;
};

export type ObpTier = {
  adults: number;
  children: number;
  rate: string;
  reduction_from_normal?: string;
};

export type ObpPolicy = {
  mode: string;
  base_adults: number;
  adult_delta: string;
  child_fee: string;
  max_adults: number;
  primary_occupancy_adults: number;
  anchor_adults?: number;
  normal_rate?: string;
  tiers_at_default_rate: ObpTier[];
};

export type SalesChannel = "direct" | "booking_com" | "airbnb";

export const SALES_CHANNEL_STORAGE_KEY = "stay.reception.pricingSalesChannel";

export type ChannelRateDay = {
  unit_id: number;
  unit_code: string;
  sales_channel?: SalesChannel;
  rate_plan_code: string;
  rate_plan_title: string;
  currency: string;
  date: string;
  rate: string;
  stop_sell: boolean;
  min_stay_arrival: number;
  obp_tiers?: ObpTier[];
  channex_push_rate?: string;
  obp_primary_occupancy_adults?: number;
  obp_anchor_adults?: number;
  obp_normal_rate?: string;
};

export type ChannelCalendarAri = {
  availability: ChannelAvailabilityDay[];
  rates: ChannelRateDay[];
};
