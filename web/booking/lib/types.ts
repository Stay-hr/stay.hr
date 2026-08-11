export type TenantSummary = {
  id: number;
  name: string;
  slug: string;
  status: string;
  timezone: string;
  default_language: string;
};

export type PropertySummary = {
  id: number;
  name: string;
  slug: string;
  address: string;
  contact: Record<string, unknown>;
  branding: Record<string, unknown>;
  timezone: string;
  language: string;
};

export type SiteContext = {
  tenant: TenantSummary;
  property: PropertySummary | null;
  domain_type: string;
  branding: Record<string, unknown>;
  languages: string[];
  default_language: string;
};

export type PublicUnit = {
  id: number;
  property_slug: string;
  code: string;
  name: string;
  capacity_max_guests: number;
  capacity_adults: number;
  capacity_children: number;
  capacity_infants: number;
  is_active: boolean;
};

export type AvailabilityUnit = {
  unit_id: number;
  unit_code: string;
  property_slug: string;
  blocked_periods: Array<{
    booking_code: string;
    check_in: string;
    check_out: string;
    status: string;
  }>;
};

export type AvailabilityResponse = {
  from: string;
  to: string;
  units: AvailabilityUnit[];
};

export type ReservationCreateResponse = {
  id: number;
  booking_code: string;
  status: string;
  check_in: string;
  check_out: string;
  property_slug: string;
};

export type ReservationStatusResponse = {
  booking_code: string;
  status: string;
  check_in: string;
  check_out: string;
  property_slug: string;
  unit_code: string;
  booker_name: string;
};

export type GuestCheckInGuestFields = {
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  date_of_birth: string | null;
  document_number: string;
  nationality: string;
  sex: string;
  address: string;
  document_type: string;
};

export type FieldConfidence = Record<string, "high" | "medium" | "low">;

export type GuestCheckInSlot = {
  position: number;
  guest_id: number;
  status: string;
  missing_fields: string[];
  field_errors?: Record<string, string>;
  field_confidence?: FieldConfidence;
  guest: GuestCheckInGuestFields;
};

export type GuestCheckInSessionResponse = {
  status: string;
  effective_status: string;
  required_slots: number;
  ready_slots: number;
  can_complete: boolean;
  waiting_positions: number[];
  ops_version: number;
  expected_checkin_adults: number | null;
  adults_count: number | null;
  booking_code: string;
  property_name: string;
  check_in: string;
  check_out: string;
  opens_at: string;
  expires_at: string;
  slots: GuestCheckInSlot[];
};

export type GuestCheckInProgressResponse = {
  status: string;
  effective_status: string;
  required_slots: number;
  ready_slots: number;
  can_complete: boolean;
  ops_version?: number;
  expected_checkin_adults?: number | null;
  adults_count?: number | null;
};

export type GuestCheckInJobResponse = {
  job_id: number;
  status: string;
  position: number;
  error_message?: string;
  guest_preview?: GuestCheckInGuestFields;
  field_confidence?: FieldConfidence;
  applied?: boolean;
  identity_status?: string;
  existing_guest_id?: number;
  existing_guest_name?: string;
  detail?: string;
  /** OCR found more people than expected_checkin_adults / OTA adults. */
  occupancy_status?: string;
  persons_detected?: number;
  expected_persons?: number;
  effective_status?: string;
  ready_slots?: number;
  can_complete?: boolean;
  ops_version?: number;
  expected_checkin_adults?: number | null;
  adults_count?: number | null;
  slot?: GuestCheckInSlot;
  slots?: GuestCheckInSlot[];
};
