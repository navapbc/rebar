## Copy Needs

schema_version: 1

- stable_id: eligibility-header
  type: heading
  location: H1 heading at the top of the eligibility questions screen
  page: eligibility_screen
  validation_rule: Must be ≤ 60 characters and must not use the word "qualify"

- stable_id: upload-error-too-large
  type: error
  location: Inline error message displayed below the file upload input when the selected file exceeds the size limit
  page: document_upload
  validation_rule: Must be ≤ 120 characters; must state the size limit explicitly; must offer a corrective action

- stable_id: submit-button
  type: button
  location: Primary CTA button at the bottom of the review screen
  page: review_screen
  validation_rule: Must be ≤ 30 characters; must use active voice; must not use the word "submit" — prefer "Send application"
