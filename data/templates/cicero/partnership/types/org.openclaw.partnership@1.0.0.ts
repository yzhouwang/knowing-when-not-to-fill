/* eslint-disable @typescript-eslint/no-empty-interface */
// Generated code for namespace: org.openclaw.partnership@1.0.0

// imports
import {IConcept} from './concerto@1.0.0';

// interfaces
export interface IPartnershipData extends IConcept {
 partyAName: string;
 partyALegalRep: string;
 partyAAddress: string;
 partyAContact: string;
 partyBName: string;
 partyBLegalRep: string;
 partyBAddress: string;
 partyBContact: string;
 targetCountry: string;
 localCurrency: string;
 partnershipName: string;
 registeredCapital: string;
 businessScope: string;
 operationTermYears: number;
 partyACapitalAmount: string;
 partyBCapitalAmount: string;
 capitalCurrency: string;
 profitDistributionDays: number;
 meetingFrequencyMonths: number;
 financialReportingDays: number;
 expenditureThresholdUSD: string;
 contractThresholdUSD: string;
 settlementThresholdUSD: string;
 dataProtectionLaw: string;
 confidentialityTermYears: number;
 governingLaw: string;
 arbitrationBody: string;
 terminationNoticeDays: number;
 breachCurePeriodDays: number;
 effectiveDate: string;
}
