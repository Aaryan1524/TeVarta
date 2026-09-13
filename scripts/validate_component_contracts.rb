#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

ROOT = File.expand_path("..", __dir__)
CONTRACT_PATH = File.join(ROOT, "contracts/components/v1/component_contracts.yaml")
FIXTURE_DIR = File.join(ROOT, "fixtures/components/v1")

def load_yaml(path)
  YAML.load_file(path)
rescue Psych::Exception => error
  abort "Invalid YAML in #{path}: #{error.message}"
end

def require_keys!(document, keys, name)
  missing = keys.reject { |key| document.key?(key) }
  abort "#{name} missing required keys: #{missing.join(', ')}" unless missing.empty?
end

contract = load_yaml(CONTRACT_PATH)
abort "Unexpected schema version" unless contract["schema_version"] == "v1-draft"
abort "Contract must remain a founder-review draft" unless contract["status"] == "pending_founder_approval"

expected_contracts = %w[
  SearchProvider
  EventCompiler
  NamedGraphOperation
  Scoring
  GraphExpansionStatus
  PreparedGraphRead
  FeedWrite
  NotificationPayload
  UsageRecord
]
missing_contracts = expected_contracts - contract.fetch("contracts").keys
abort "Missing component contracts: #{missing_contracts.join(', ')}" unless missing_contracts.empty?

fixtures = {
  "discovered_article.yaml" => %w[request_id candidates usage],
  "ambiguous_entity.yaml" => %w[surface_form resolution candidates resolution_miss_written],
  "compiled_event.yaml" => %w[event_id revision canonical_title severity_class claims proposed_edges usage],
  "temporal_edge_revision.yaml" => %w[operation_name request_id parameters revision_effect],
  "winning_path.yaml" => %w[user_id event_id score components path_json slot_type slot_position channel],
  "merged_event.yaml" => %w[surviving_event_id merged_event_id merge_reason revision effect],
  "notification.yaml" => %w[device_id event_id channel title body collapse_id]
}

fixtures.each do |file, required_keys|
  path = File.join(FIXTURE_DIR, file)
  abort "Missing fixture: #{file}" unless File.file?(path)
  require_keys!(load_yaml(path), required_keys, file)
end

edge = load_yaml(File.join(FIXTURE_DIR, "temporal_edge_revision.yaml")).fetch("parameters")
require_keys!(edge, contract.fetch("contracts").fetch("NamedGraphOperation").fetch("temporal_edge_required"), "temporal edge")
allowed_relations = contract.fetch("common").fetch("allowed_relation_types")
abort "Fixture uses a non-allowlisted relation" unless allowed_relations.include?(edge.fetch("relation_type"))
abort "Temporal edge provenance cannot be empty" if edge.fetch("source_ids").empty?

components = load_yaml(File.join(FIXTURE_DIR, "winning_path.yaml")).fetch("components")
expected_components = contract.fetch("contracts").fetch("Scoring").fetch("response").fetch("relevance_components")
require_keys!(components, expected_components, "winning path relevance components")

puts "Component contracts and #{fixtures.length} fixtures are structurally valid."
