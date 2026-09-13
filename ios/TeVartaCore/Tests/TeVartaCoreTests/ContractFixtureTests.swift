import Foundation
import Testing
@testable import TeVartaCore

struct ContractFixtureTests {
    private func fixture(_ name: String) throws -> Data {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0 ..< 5 { root.deleteLastPathComponent() }
        return try Data(contentsOf: root.appending(path: "fixtures/v1/\(name)"))
    }

    @Test func sharedFixturesDecodeWithSwiftModels() throws {
        _ = try ContractDecoder.json.decode(AuthResponse.self, from: fixture("auth_success.json"))
        _ = try ContractDecoder.json.decode(EntitySearchResponse.self, from: fixture("entity_search_ambiguous.json"))
        _ = try ContractDecoder.json.decode(EntitySearchResponse.self, from: fixture("entity_search_success.json"))
        _ = try ContractDecoder.json.decode(SeedResponse.self, from: fixture("seed_success.json"))
        _ = try ContractDecoder.json.decode(SeedsStatusResponse.self, from: fixture("seeds_status.json"))
        _ = try ContractDecoder.json.decode(FeedResponse.self, from: fixture("feed_success.json"))
        _ = try ContractDecoder.json.decode(FeedResponse.self, from: fixture("feed_empty.json"))
        _ = try ContractDecoder.json.decode(LoadingResponse.self, from: fixture("loading.json"))
        _ = try ContractDecoder.json.decode(EventDetailResponse.self, from: fixture("event_success.json"))
        _ = try ContractDecoder.json.decode(EventDetailResponse.self, from: fixture("event_merged.json"))
        _ = try ContractDecoder.json.decode(GraphResponse.self, from: fixture("graph.json"))
        _ = try ContractDecoder.json.decode(EntityResponse.self, from: fixture("entity_success.json"))
        _ = try ContractDecoder.json.decode(ProfileResponse.self, from: fixture("profile.json"))
        _ = try ContractDecoder.json.decode(DeviceResponse.self, from: fixture("device_success.json"))
        _ = try ContractDecoder.json.decode(FeedbackResponse.self, from: fixture("feedback_success.json"))
        _ = try ContractDecoder.json.decode(AccountDeletionResponse.self, from: fixture("account_deletion.json"))
        _ = try ContractDecoder.json.decode(ErrorEnvelope.self, from: fixture("errors/expired_auth.json"))
    }

    @Test func mergedEventPreservesTheRequestedIDAndCanonicalEvent() throws {
        let merged = try ContractDecoder.json.decode(EventDetailResponse.self, from: fixture("event_merged.json"))

        #expect(merged.requestedEventId == "evt_fremont_initial")
        #expect(merged.event.eventId == "evt_fremont_halt")
        #expect(merged.mergedFrom == ["evt_fremont_initial"])
    }
}
