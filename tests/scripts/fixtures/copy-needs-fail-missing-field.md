## Copy Needs

schema_version: 1

- stable_id: confirmation-message
  type: confirmation
  location: Body text on the confirmation page after successful submission
  page: confirmation_page

- stable_id: dashboard-greeting
  type: heading
  location: H1 greeting on the user dashboard
  page: dashboard
  validation_rule: Must be ≤ 50 characters; must be personalized with the user's first name
